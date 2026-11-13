"""读取详情页时，容忍客户端继续导航造成的短暂执行上下文失效。"""
from __future__ import annotations

import time

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from zhihu_collector.browser.page_guard import inspect_page
from zhihu_collector.errors import CollectorError
from zhihu_collector.utils.logger import get_logger

logger = get_logger("page_content")
_NAVIGATION_ERRORS = (
    "page is navigating and changing the content",
    "execution context was destroyed",
    "cannot find context with specified id",
)


class PageContentNotReady(CollectorError):
    """已读到页面，但目标回答尚未准备好。"""


def read_page_content(page, controller, *, network_errors=None, is_ready=None, attempts=12, timeout_ms=15000):
    """仅重试导航瞬态错误；登录、验证、取消和永久错误立即向调用方传播。

    不重新访问网址，避免重复导航打断页面；等待 DOM 就绪后重新读取当前文档。
    次数与时间预算约束重试，单次 Playwright 调用仍使用浏览器自身的超时机制。
    """
    if attempts < 1 or timeout_ms <= 0:
        raise ValueError("attempts 和 timeout_ms 必须大于 0")
    deadline = time.monotonic() + timeout_ms / 1000
    last_error = None
    for attempt in range(attempts):
        controller.checkpoint()
        # 先判断明确的登录/验证跳转，避免残留正文或导航异常遮住真正原因。
        inspect_page(page, found_content=1, network_errors=network_errors)
        try:
            content = page.content()
            controller.checkpoint()
            inspect_page(page, found_content=1, network_errors=network_errors)
            if is_ready is None or is_ready(content):
                return content
            inspect_page(page, found_content=0, network_errors=network_errors)
            last_error = None
        except PlaywrightError as exc:
            if not any(hint in str(exc).lower() for hint in _NAVIGATION_ERRORS):
                raise
            last_error = exc
        controller.checkpoint()
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if attempt == attempts - 1 or remaining_ms <= 0:
            break
        logger.info("详情页尚未就绪，等待后重读正文（第 %s 次）", attempt + 1)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=min(1000, remaining_ms))
        except PlaywrightTimeout:
            pass
        except PlaywrightError as exc:
            if not any(hint in str(exc).lower() for hint in _NAVIGATION_ERRORS):
                raise
        controller.checkpoint()
        inspect_page(page, found_content=1, network_errors=network_errors)
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        page.wait_for_timeout(min(400, remaining_ms))
        if time.monotonic() >= deadline:
            break
    if last_error is None:
        raise PageContentNotReady("目标回答正文尚未加载完成，等待超时，请稍后重试。")
    raise CollectorError("页面持续跳转，暂时无法读取稳定正文，请稍后重试。") from last_error
