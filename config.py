"""
双引擎异步搜索与自动下载系统 — 全局配置
==============================================
所有可调参数集中管理，支持通过环境变量覆盖。
"""

import os
import logging

# ── PostgreSQL（种子数据库，只读操作）─────────────────────────────
PG_HOST = os.getenv("PG_HOST", "127.0.0.1")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB = os.getenv("PG_DB", "bitmagnet")
PG_USER = os.getenv("PG_USER", "bitmagnet")
PG_PASSWORD = os.getenv("PG_PASSWORD", "bitmagnet")
PG_MIN_CONN = int(os.getenv("PG_MIN_CONN", "1"))
PG_MAX_CONN = int(os.getenv("PG_MAX_CONN", "10"))          # 连接池大小（挖掘+搜索并发需要）

# ── SQLite（内部状态存储：缓存、断点、订阅、日志）────────────────
SQLITE_PATH = os.getenv("SQLITE_PATH", "/data/search_engine.db")

# ── 新内容雷达 ────────────────────────────────────────────────────
RADAR_INTERVAL_SEC = int(os.getenv("RADAR_INTERVAL_SEC", "10"))    # 扫描间隔（秒）
RADAR_BATCH_SIZE = int(os.getenv("RADAR_BATCH_SIZE", "500"))       # 每次扫描条数

# ── 旧数据挖掘机 ──────────────────────────────────────────────────
MINER_BATCH_SIZE = int(os.getenv("MINER_BATCH_SIZE", "2000"))      # 每批查询条数
MINER_SLEEP_SEC = float(os.getenv("MINER_SLEEP_SEC", "2.0"))       # 每批后休眠秒数
MINER_MAX_RUNTIME_SEC = int(os.getenv("MINER_MAX_RUNTIME_SEC", "3600"))  # 单次任务最长运行时间

# ── 搜索缓存 ──────────────────────────────────────────────────────
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "86400"))           # 缓存有效期（默认 24 小时）
SEARCH_PAGE_SIZE = int(os.getenv("SEARCH_PAGE_SIZE", "50"))        # 搜索单页条数

# ── qBittorrent ───────────────────────────────────────────────────
QB_HOST = os.getenv("QB_HOST", "127.0.0.1")
QB_PORT = int(os.getenv("QB_PORT", "8080"))
QB_USER = os.getenv("QB_USER", "admin")
QB_PASSWORD = os.getenv("QB_PASSWORD", "adminadmin")
QB_CATEGORY = os.getenv("QB_CATEGORY", "radar")                    # 自动添加的分类/标签
QB_SAVE_PATH = os.getenv("QB_SAVE_PATH", "/mnt/Storage1/downloads")

# ── 自动清理 ──────────────────────────────────────────────────────
CLEANUP_INTERVAL_SEC = int(os.getenv("CLEANUP_INTERVAL_SEC", "300"))      # 清理检查间隔
CLEANUP_MIN_RATIO = float(os.getenv("CLEANUP_MIN_RATIO", "2.0"))          # 最低分享率
CLEANUP_MIN_SEED_MIN = int(os.getenv("CLEANUP_MIN_SEED_MIN", "1440"))     # 最低做种时间（分钟）
CLEANUP_DELETE_FILES = os.getenv("CLEANUP_DELETE_FILES", "true").lower() == "true"

# ── API 服务器 ────────────────────────────────────────────────────
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "3001"))

# ── 截图预览 ──────────────────────────────────────────────────────
# 外部截图服务 URL 模板，{code} 会被替换为解析出的编号
SCREENSHOT_URLS = os.getenv("SCREENSHOT_URLS", 
    "https://pics.dmm.co.jp/digital/video/{code}/{code}pl.jpg,"
    "https://pics.dmm.co.jp/mono/movie/adult/{code}/{code}pl.jpg"
).split(",")

# whatslink.info 截图服务
WHATSLINK_ENABLED = os.getenv("WHATSLINK_ENABLED", "true").lower() == "true"
WHATSLINK_API = os.getenv("WHATSLINK_API", "https://whatslink.info/api/v1/link")

# 图片代理出口（HTTP 或 SOCKS5）
# 示例: http://127.0.0.1:7890  /  socks5://127.0.0.1:1080
IMAGE_PROXY = os.getenv("IMAGE_PROXY", "")

# ── 日志 ──────────────────────────────────────────────────────────
LOG_DIR = os.getenv("LOG_DIR", "/data/logs")
LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
LOG_FILE_LEVEL = getattr(logging, os.getenv("LOG_FILE_LEVEL", "INFO").upper(), logging.INFO)
LOG_CONSOLE_LEVEL = getattr(logging, os.getenv("LOG_CONSOLE_LEVEL", "DEBUG").upper(), logging.DEBUG)

# ── 容错重试 ──────────────────────────────────────────────────────
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))                # 最大重试次数
RETRY_BASE_SEC = float(os.getenv("RETRY_BASE_SEC", "1.0"))      # 退避基数（秒）
RETRY_MAX_SEC = float(os.getenv("RETRY_MAX_SEC", "120.0"))      # 退避上限（秒）
