"""
数据库层
=======
- PostgreSQL 连接池（只读，查询种子数据）
- SQLite（本地状态存储：断点、缓存、订阅、日志）
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.pool

import config
from logger_setup import log


# ══════════════════════════════════════════════════════════════════
# PostgreSQL 连接池（SimpleConnectionPool，线程安全）
# ══════════════════════════════════════════════════════════════════

_pg_pool: Optional[psycopg2.pool.SimpleConnectionPool] = None
_pg_lock = threading.Lock()


def _build_pg_pool():
    """构建 PostgreSQL 连接池。"""
    global _pg_pool
    with _pg_lock:
        if _pg_pool is not None:
            return
        _pg_pool = psycopg2.pool.SimpleConnectionPool(
            config.PG_MIN_CONN,
            config.PG_MAX_CONN,
            host=config.PG_HOST,
            port=config.PG_PORT,
            dbname=config.PG_DB,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            connect_timeout=10,
            options="-c statement_timeout=30000",  # 单条查询超时 30s
        )
        log.info("PostgreSQL 连接池已创建 (min=%d, max=%d)", config.PG_MIN_CONN, config.PG_MAX_CONN)


def get_pg_conn():
    """从连接池获取一个 PostgreSQL 连接。"""
    if _pg_pool is None:
        _build_pg_pool()
    return _pg_pool.getconn()


def put_pg_conn(conn):
    """归还连接。"""
    if _pg_pool is not None and conn is not None:
        _pg_pool.putconn(conn)


@contextmanager
def pg_cursor(autocommit=True):
    """上下文管理器：自动获取/归还连接，返回 cursor。"""
    conn = None
    try:
        conn = get_pg_conn()
        conn.autocommit = autocommit
        cur = conn.cursor()
        yield cur
        cur.close()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        put_pg_conn(conn)


# ══════════════════════════════════════════════════════════════════
# SQLite（内部状态，线程安全写入）
# ══════════════════════════════════════════════════════════════════

_sqlite_local = threading.local()


def _get_sqlite_conn() -> sqlite3.Connection:
    """获取当前线程的 SQLite 连接（线程本地存储）。"""
    if not hasattr(_sqlite_local, "conn") or _sqlite_local.conn is None:
        import os
        os.makedirs(os.path.dirname(config.SQLITE_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(config.SQLITE_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _sqlite_local.conn = conn
    return _sqlite_local.conn


def init_sqlite():
    """创建所有内部状态表（幂等）。"""
    conn = _get_sqlite_conn()
    conn.executescript("""
    -- 搜索缓存：key=关键词+过滤条件 MD5, value=JSON 结果
    CREATE TABLE IF NOT EXISTS search_cache (
        cache_key   TEXT PRIMARY KEY,
        keyword     TEXT NOT NULL,
        results     TEXT NOT NULL,       -- JSON
        total_count INTEGER DEFAULT 0,
        created_at  REAL NOT NULL,       -- epoch timestamp
        hit_count   INTEGER DEFAULT 1
    );

    -- 旧数据扫描断点
    CREATE TABLE IF NOT EXISTS miner_checkpoint (
        keyword         TEXT PRIMARY KEY,
        last_cursor_ts  TEXT NOT NULL,           -- ISO timestamp 格式的断点
        last_info_hash  TEXT,                    -- hex hash，用于精确恢复
        scanned_count   INTEGER DEFAULT 0,
        match_count     INTEGER DEFAULT 0,
        estimated_total INTEGER DEFAULT 0,       -- 估算总数据量（修复进度不准）
        status          TEXT DEFAULT 'pending',  -- pending / running / paused / done
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL
    );

    -- 雷达订阅关键词
    CREATE TABLE IF NOT EXISTS subscriptions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword     TEXT NOT NULL,
        enabled     INTEGER DEFAULT 1,
        created_at  REAL NOT NULL
    );

    -- 雷达进度指针
    CREATE TABLE IF NOT EXISTS radar_pointer (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        last_created_at TEXT NOT NULL,
        last_info_hash  TEXT,
        updated_at      REAL NOT NULL
    );

    -- 推送日志（推送到 qBittorrent 的记录）
    CREATE TABLE IF NOT EXISTS push_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        info_hash   TEXT NOT NULL,
        name        TEXT NOT NULL,
        size        BIGINT,
        keyword     TEXT,
        source      TEXT NOT NULL,       -- radar / miner
        pushed_at   REAL NOT NULL,
        success     INTEGER DEFAULT 1
    );

    -- 清理日志
    CREATE TABLE IF NOT EXISTS cleanup_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        info_hash   TEXT NOT NULL,
        name        TEXT NOT NULL,
        ratio       REAL,
        seed_min    INTEGER,
        cleaned_at  REAL NOT NULL,
        deleted_files INTEGER DEFAULT 0
    );

    -- 批量搜索任务
    CREATE TABLE IF NOT EXISTS search_tasks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword         TEXT NOT NULL,
        filter_time     TEXT DEFAULT '',
        filter_size     TEXT DEFAULT '',
        sort_type       TEXT DEFAULT 'date',
        status          TEXT DEFAULT 'pending',   -- pending/running/done/failed
        progress_pct    REAL DEFAULT 0,            -- 真实进度 0-100
        progress_msg    TEXT DEFAULT '',
        total_found     INTEGER DEFAULT 0,
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL
    );
    """)
    # 兼容升级：为已有的 miner_checkpoint 添加 estimated_total 列
    try:
        conn.execute("ALTER TABLE miner_checkpoint ADD COLUMN estimated_total INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    log.info("SQLite 内部数据库已初始化: %s", config.SQLITE_PATH)
