"""
重试与容错工具
===============
指数退避重试装饰器，用于所有外部 API 调用。
"""

import time
import random
import functools

import config
from logger_setup import log


def retry_with_backoff(max_retries=None, base_sec=None, max_sec=None,
                       retry_on=(Exception,)):
    """
    指数退避重试装饰器。

    用法:
        @retry_with_backoff(retry_on=(requests.exceptions.Timeout,))
        def call_api():
            ...
    """
    _max_retries = max_retries or config.MAX_RETRIES
    _base_sec = base_sec or config.RETRY_BASE_SEC
    _max_sec = max_sec or config.RETRY_MAX_SEC

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(_max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retry_on as e:
                    last_exc = e
                    if attempt == _max_retries:
                        log.error("[%s] 已达最大重试次数 %d，放弃。%s",
                                  func.__name__, _max_retries, str(e)[:200])
                        raise
                    sleep_sec = min(_base_sec * (2 ** attempt) + random.uniform(0, 1),
                                    _max_sec)
                    log.warning("[%s] 第 %d/%d 次重试，等待 %.1f 秒... 原因: %s",
                                func.__name__, attempt + 1, _max_retries,
                                sleep_sec, str(e)[:150])
                    time.sleep(sleep_sec)
            raise last_exc  # type: ignore
        return wrapper
    return decorator
