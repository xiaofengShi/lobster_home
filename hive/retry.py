#!/usr/bin/env python3
"""
🔄 蜂巢重试机制

IoT 环境网络抖动常见，提供统一重试策略：
- API 调用自动重试（指数退避）
- 区分可恢复 vs 不可恢复错误
- 超时保护
"""

import time
import functools
import requests


# 可恢复的错误（值得重试）
RETRIABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    ConnectionResetError,
    TimeoutError,
    OSError,
)

# 可恢复的 HTTP 状态码
RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def retry(max_retries=2, base_delay=1.0, max_delay=10.0, retriable=None):
    """重试装饰器（指数退避）

    Args:
        max_retries: 最大重试次数（不含首次）
        base_delay: 初始等待秒数
        max_delay: 最大等待秒数
        retriable: 可重试异常元组，默认 RETRIABLE_EXCEPTIONS
    """
    if retriable is None:
        retriable = RETRIABLE_EXCEPTIONS

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    result = func(*args, **kwargs)
                    # 检查 requests.Response 的状态码
                    if isinstance(result, requests.Response) and result.status_code in RETRIABLE_STATUS_CODES:
                        if attempt < max_retries:
                            delay = min(base_delay * (2 ** attempt), max_delay)
                            # 尊重 Retry-After header
                            retry_after = result.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    delay = max(delay, float(retry_after))
                                except ValueError:
                                    pass
                            time.sleep(delay)
                            continue
                    return result
                except retriable as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        time.sleep(delay)
                    else:
                        raise
                except Exception:
                    # 不可恢复错误，直接抛出
                    raise
            if last_exception:
                raise last_exception
        return wrapper
    return decorator


def resilient_request(method, url, max_retries=2, **kwargs):
    """带重试的 requests 调用

    Args:
        method: "get", "post", "put" 等
        url: 请求 URL
        max_retries: 最大重试次数
        **kwargs: 传给 requests 的参数

    Returns:
        requests.Response

    Raises:
        最后一次失败的异常
    """
    kwargs.setdefault("timeout", 15)
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            resp = getattr(requests, method)(url, **kwargs)
            if resp.status_code in RETRIABLE_STATUS_CODES and attempt < max_retries:
                delay = min(1.0 * (2 ** attempt), 10.0)
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
                time.sleep(delay)
                continue
            return resp
        except RETRIABLE_EXCEPTIONS as e:
            last_exception = e
            if attempt < max_retries:
                time.sleep(min(1.0 * (2 ** attempt), 10.0))
            else:
                raise
    if last_exception:
        raise last_exception
