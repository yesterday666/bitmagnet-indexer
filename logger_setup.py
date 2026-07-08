"""
日志系统
=======
INFO 及以上 → 文件（按天轮转）
DEBUG 及以上 → 控制台
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler

from config import LOG_DIR, LOG_LEVEL, LOG_FILE_LEVEL, LOG_CONSOLE_LEVEL


def setup_logging(name: str = "search_engine") -> logging.Logger:
    """初始化日志系统，返回根 logger。"""
    os.makedirs(LOG_DIR, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # 根 logger 捕获所有级别，由 handler 过滤

    # ── 文件 handler ──────────────────────────────────────────
    file_path = os.path.join(LOG_DIR, "search_engine.log")
    fh = TimedRotatingFileHandler(file_path, when="midnight", interval=1, backupCount=30,
                                  encoding="utf-8")
    fh.setLevel(LOG_FILE_LEVEL)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)-15s | %(message)s"
    ))
    logger.addHandler(fh)

    # ── 控制台 handler ────────────────────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(LOG_CONSOLE_LEVEL)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(threadName)-15s | %(message)s",
        datefmt="%H:%M:%S"
    ))
    logger.addHandler(ch)

    return logger


# 全局 logger 实例
log = setup_logging()
