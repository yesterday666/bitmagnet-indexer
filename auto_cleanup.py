"""
自动清理与做种管理
===================
定期检查 qBittorrent 中已完成的种子任务。
当分享率达到设定值或做种时间达标后，自动删除任务并清理本地文件。

清理条件（同时满足任一）：
- 分享率 >= CLEANUP_MIN_RATIO
- 做种时间 >= CLEANUP_MIN_SEED_MIN 分钟
"""

import time
import threading

import config
from database import _get_sqlite_conn
from qb_client import qb
from logger_setup import log


class AutoCleaner:
    """自动清理服务。"""

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None

    def _cleanup_loop(self):
        """清理循环。"""
        log.info("自动清理服务已启动，间隔 %d 秒", config.CLEANUP_INTERVAL_SEC)

        while self._running:
            try:
                # 获取我们分类下的所有种子
                torrents = qb.get_torrents(category=config.QB_CATEGORY)
                if not torrents:
                    # 也检查 miner_ 前缀的分类
                    all_torrents = qb.get_torrents()
                    torrents = [t for t in all_torrents
                                if t.get("category", "").startswith(
                                    (config.QB_CATEGORY, "miner_"))]
                if not torrents:
                    log.debug("清理：无可清理的种子")
                    time.sleep(config.CLEANUP_INTERVAL_SEC)
                    continue

                to_delete = []
                for t in torrents:
                    # 只处理已完成的种子
                    state = t.get("state", "")
                    if state not in ("uploading", "stalledUP", "pausedUP", "checkingUP",
                                     "queuedUP", "forcedUP", "moving"):
                        continue

                    ratio = float(t.get("ratio", 0))
                    seeding_time_min = int(t.get("seeding_time", 0)) // 60
                    name = t.get("name", "")
                    hash_val = t.get("hash", "")

                    should_delete = False
                    reason = ""

                    # 条件 1：分享率达标
                    if ratio >= config.CLEANUP_MIN_RATIO:
                        should_delete = True
                        reason = f"分享率 {ratio:.2f} >= {config.CLEANUP_MIN_RATIO}"

                    # 条件 2：做种时间达标
                    elif seeding_time_min >= config.CLEANUP_MIN_SEED_MIN:
                        should_delete = True
                        reason = f"做种时间 {seeding_time_min}min >= {config.CLEANUP_MIN_SEED_MIN}min"

                    if should_delete:
                        to_delete.append((hash_val, name, ratio, seeding_time_min, reason))
                        log.info("清理候选: %s | %s", name[:60], reason)

                # 执行删除
                if to_delete:
                    hashes = [h[0] for h in to_delete]
                    success = qb.delete_torrents(hashes, delete_files=config.CLEANUP_DELETE_FILES)

                    conn = _get_sqlite_conn()
                    for h in to_delete:
                        conn.execute(
                            """INSERT INTO cleanup_log (info_hash, name, ratio, seed_min, cleaned_at, deleted_files)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (h[0], h[1], h[2], h[3], time.time(),
                             1 if config.CLEANUP_DELETE_FILES else 0)
                        )
                    conn.commit()

                    log.info("清理完成: 删除了 %d 个种子 (delete_files=%s)",
                             len(to_delete), config.CLEANUP_DELETE_FILES)
                else:
                    log.debug("清理：本轮无需清理的种子")

            except Exception as e:
                log.error("清理异常: %s", e, exc_info=True)

            time.sleep(config.CLEANUP_INTERVAL_SEC)

        log.info("自动清理服务已停止")

    # ── 启停控制 ────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._cleanup_loop, name="Cleaner", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=15)

    def get_status(self) -> dict:
        conn = _get_sqlite_conn()
        total_cleaned = conn.execute("SELECT COUNT(*) as c FROM cleanup_log").fetchone()["c"]
        recent = conn.execute(
            "SELECT * FROM cleanup_log ORDER BY cleaned_at DESC LIMIT 10"
        ).fetchall()
        return {
            "running": self._running,
            "interval_sec": config.CLEANUP_INTERVAL_SEC,
            "min_ratio": config.CLEANUP_MIN_RATIO,
            "min_seed_min": config.CLEANUP_MIN_SEED_MIN,
            "delete_files": config.CLEANUP_DELETE_FILES,
            "total_cleaned": total_cleaned,
            "recent_cleaned": [
                {
                    "name": r["name"][:60],
                    "ratio": r["ratio"],
                    "seed_min": r["seed_min"],
                    "cleaned_at": r["cleaned_at"],
                }
                for r in recent
            ],
        }


# 全局单例
cleaner = AutoCleaner()
