"""在知乎自己的列表页上滚动，直到没有新内容。"""

from __future__ import annotations

import inspect
import random
import time
from collections.abc import Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from zhihu_collector.browser.network_listener import NetworkListener
from zhihu_collector.browser.page_guard import inspect_page
from zhihu_collector.browser.navigation import navigation_once
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.paging import ScrollState
from zhihu_collector.config import Settings
from zhihu_collector.models import ContentRecord, dedupe_records
from zhihu_collector.selectors.zhihu import ZhihuSelectors
from zhihu_collector.utils.list_target import ListTarget, apply_bound_paging, response_matches_list, sanitize_endpoint, summarize_list_items
from zhihu_collector.utils.logger import get_logger
from zhihu_collector.utils.retry import call_with_retry

logger = get_logger("listing")

ParseFn = Callable[[list, str], list[ContentRecord]]
OnPage = Callable[..., None]


def _invoke_on_page(on_page: OnPage, emit: list[ContentRecord], totals: int | None, seen_count: int, skipped_count: int) -> None:
    try:
        sig = inspect.signature(on_page)
        params = list(sig.parameters.values())
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        accepts_4 = has_varargs or len(params) >= 4 or "skipped" in sig.parameters
    except (ValueError, TypeError):
        accepts_4 = True

    if accepts_4:
        on_page(emit, totals, seen_count, skipped_count)
    else:
        on_page(emit, totals, seen_count)


def collect_listing(
    session,
    start_url: str,
    *,
    settings: Settings,
    controller: TaskController,
    parse_fn: ParseFn,
    on_page: OnPage,
    known_versions: dict[str, tuple[str, str]] | None = None,
    update_only: bool = False,
    list_target: ListTarget | None = None,
    state_box: dict | None = None,
    on_diagnostic=None,
) -> ScrollState:
    known_versions = known_versions or {}
    listener = NetworkListener()
    page = session.new_page()
    listener.attach(page)
    page_id = f"p{id(page) & 0xFFFF:x}"
    controller.checkpoint()
    logger.info("打开列表页 %s", start_url)
    _goto(page, start_url, settings, controller)
    _dismiss_obstructions(page)
    sorted_by_time = _try_sort_by_time(page)
    _wait_for_first_payload(page, listener, controller, timeout_ms=12_000, list_target=list_target)
    state = ScrollState(max_stale_rounds=settings.max_stale_rounds)
    if state_box is not None:
        state_box["state"] = state
    page_index = 0
    while True:
        controller.checkpoint()
        page_index += 1
        payloads = listener.consume()
        html = _safe_content(page)
        selected = _payloads_for_target(payloads, list_target)
        records = dedupe_records(parse_fn(selected, html))
        item_summary = summarize_list_items(payloads, list_target) if list_target is not None else {
            "raw": 0, "identified": 0, "unparsed": 0, "unknown": 0,
        }
        state.unparsed_count += int(item_summary["unparsed"])
        state.unknown_type_count += int(item_summary["unknown"])
        decision = apply_bound_paging(state, payloads, list_target)
        totals = decision["totals"]
        errors = listener.consume_errors()
        if list_target is not None:
            errors = [error for error in errors if response_matches_list(error.get("url", ""), list_target)]
        inspect_page(page, found_content=len(records), network_errors=errors)
        seen_before = len(state.seen_ids)
        duplicates_before = state.duplicate_count
        emit = select_records_to_emit(
            records,
            state=state,
            known_versions=known_versions,
            update_only=update_only,
            sorted_by_time=sorted_by_time,
            is_end=bool(decision["is_end"]),
            has_next=decision["has_next"],
            page_says_no_more=_page_says_no_more(page),
        )
        _emit_diagnostic(
            on_diagnostic,
            page_id=page_id,
            page_index=page_index,
            list_target=list_target,
            decision=decision,
            item_summary=item_summary,
            parsed_count=len(records),
            new_count=len(state.seen_ids) - seen_before,
            duplicate_count=state.duplicate_count - duplicates_before,
            state=state,
        )
        skipped_count = getattr(state, "last_skipped_count", 0)
        _invoke_on_page(on_page, emit, int(totals) if isinstance(totals, int) else None, len(state.seen_ids), skipped_count)
        if state.reached_end:
            logger.info("列表结束：%s", state.stop_reason)
            break
        controller.checkpoint()
        before = listener.total_captured
        _scroll_once(page)
        _polite_wait(page, settings)
        _wait_for_growth(page, listener, before, timeout_ms=3_000)
    return state


def _payloads_for_target(payloads, list_target: ListTarget | None) -> list:
    selected = []
    for item in payloads:
        if getattr(item, "status", 200) >= 400:
            continue
        if list_target is not None and not response_matches_list(getattr(item, "url", "") or "", list_target):
            continue
        selected.append(item.data)
    return selected


def _emit_diagnostic(on_diagnostic, **detail) -> None:
    if on_diagnostic is None:
        return
    decision = detail["decision"]
    held = decision.get("held") or {}
    target = detail["list_target"]
    try:
        on_diagnostic({
            "kind": "page",
            "page_id": detail["page_id"],
            "page_index": detail["page_index"],
            "mode": target.kind if target is not None else "",
            "target": target.identity if target is not None else "",
            "endpoint": held.get("url") or "",
            "status": held.get("status"),
            "offset": _query_value(held.get("url") or "", "offset"),
            "cursor": sanitize_endpoint(held.get("next") or ""),
            "raw_count": detail["item_summary"]["raw"],
            "parsed_count": detail["parsed_count"],
            "new_count": detail["new_count"],
            "duplicate_count": detail["duplicate_count"],
            "unparsed_count": detail["item_summary"]["unparsed"],
            "unknown_count": detail["item_summary"]["unknown"],
            "is_end_present": held.get("is_end_present"),
            "is_end": held.get("is_end"),
            "next_present": held.get("next_present"),
            "has_next": held.get("has_next"),
            "totals": held.get("totals"),
            "totals_source": held.get("url") or "",
            "ignored_foreign": decision.get("ignored") or 0,
            "end_evidence": detail["state"].end_evidence,
            "stop_reason": detail["state"].stop_reason,
        })
    except Exception:
        logger.exception("分页诊断回调失败")


def _query_value(url: str, key: str) -> str:
    if not url or "?" not in url:
        return ""
    from urllib.parse import parse_qs

    return (parse_qs(url.split("?", 1)[1]).get(key) or [""])[0]


def select_records_to_emit(
    records: list[ContentRecord],
    *,
    state: ScrollState,
    known_versions: dict[str, tuple[str, str]],
    update_only: bool = False,
    sorted_by_time: bool = False,
    is_end: bool = False,
    has_next: bool | None = None,
    page_says_no_more: bool = False,
) -> list[ContentRecord]:
    """旧内容首次出现只表示这一页有进展，不表示需要重新保存。"""
    ordered: list[ContentRecord] = []
    page_ids: list[str] = []
    seen_on_page: set[str] = set()
    unchanged: set[str] = set()
    for record in records:
        content_id = record.zhihu_content_id
        if not content_id or content_id in seen_on_page:
            if content_id and content_id in seen_on_page:
                state.duplicate_count += 1
            continue
        seen_on_page.add(content_id)
        page_ids.append(content_id)
        ordered.append(record)
        if content_id in state.seen_ids:
            state.duplicate_count += 1
        previous = known_versions.get(content_id)
        if previous is not None and previous == _version(record) and not record.content_incomplete:
            unchanged.add(content_id)
    fresh_ids = set(
        state.observe_page(
            page_ids,
            is_end=is_end,
            has_next=has_next,
            page_says_no_more=page_says_no_more,
            unchanged_ids=unchanged if update_only else None,
            update_only=update_only,
            sorted_by_time=sorted_by_time,
        )
    )
    fresh_unchanged = {cid for cid in fresh_ids if cid in unchanged}
    state.skipped_ids.update(fresh_unchanged)
    state.last_skipped_count = len(fresh_unchanged)
    return [
        record
        for record in ordered
        if record.zhihu_content_id in fresh_ids and record.zhihu_content_id not in unchanged
    ]


def _version(record: ContentRecord) -> tuple[str, str]:
    from zhihu_collector.utils.time_utils import unix_to_iso

    return (unix_to_iso(record.updated_time) or "", record.content_hash)


class _TransientNavigationError(Exception):
    pass


def _goto(
    page, url: str, settings: Settings, controller: TaskController, *,
    timeout_ms: int | None = None, transient_only: bool = False,
) -> None:
    current_attempt = 0
    total_attempts = max(1, settings.retry_count)

    def attempt() -> None:
        nonlocal current_attempt
        controller.checkpoint()
        current_attempt += 1
        try:
            navigation_once(page, url, timeout_ms=timeout_ms,
                            attempt=current_attempt, attempts=total_attempts)
        except PlaywrightError as exc:
            if not transient_only:
                raise
            # 重定向到登录/验证页面时，不因导航超时而再发一次请求。
            inspect_page(page, found_content=1)
            if isinstance(exc, PlaywrightTimeout) or any(code in str(exc) for code in (
                "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_ABORTED",
                "ERR_CONNECTION_REFUSED", "ERR_TIMED_OUT", "ERR_NETWORK_CHANGED", "ERR_EMPTY_RESPONSE",
            )):
                raise _TransientNavigationError(str(exc)) from exc
            raise

    def cancellable_wait(delay: float) -> None:
        deadline = time.monotonic() + delay
        while True:
            controller.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

    call_with_retry(
        attempt,
        attempts=total_attempts,
        base_delay_ms=settings.retry_base_delay_ms,
        retry_on=(_TransientNavigationError,) if transient_only else (PlaywrightTimeout, PlaywrightError, TimeoutError, OSError),
        sleep=cancellable_wait,
    )


def _wait_for_first_payload(page, listener: NetworkListener, controller: TaskController, timeout_ms: int, list_target: ListTarget | None = None) -> None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        controller.checkpoint()
        if list_target is not None:
            matched = any(
                response_matches_list(item.url, list_target) and item.status < 400
                for item in listener.snapshot()
            )
            if matched:
                page.wait_for_timeout(500)
                return
        elif listener.has_listing_payload or listener.last_paging is not None:
            page.wait_for_timeout(500)
            return
        page.wait_for_timeout(300)


def _wait_for_growth(page, listener: NetworkListener, before: int, timeout_ms: int) -> None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline and listener.total_captured == before:
        page.wait_for_timeout(200)


def _scroll_once(page) -> None:
    try:
        # Detail enrichment opens another tab. Zhihu suspends list lazy loading
        # in a hidden tab; restore the list before scrolling it again.
        page.bring_to_front()
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    except Exception as exc:
        logger.debug("滚动失败：%s", exc)
    try:
        locator = page.locator(ZhihuSelectors.load_more)
        if locator.count() and locator.first.is_visible():
            locator.first.click(timeout=1000)
    except Exception:
        return


def _try_sort_by_time(page) -> bool:
    for text in ("按时间排序", "最新排序"):
        try:
            locator = page.get_by_text(text, exact=False)
            if locator.count() == 0:
                continue
            locator.first.click(timeout=1500)
            page.wait_for_timeout(600)
            logger.info("已切换为按时间排序")
            return True
        except Exception:
            continue
    return False


_LIST_ROOT_SELECTORS = (
    ".Profile-main",
    ".ProfileMain",
    ".Question-main",
    ".QuestionAnswers-answers",
)
_LIST_END_SCRIPT = """
(selectors) => {
  const texts = [];
  for (const sel of selectors) {
    document.querySelectorAll(sel).forEach((node) => {
      const value = (node.innerText || "").trim();
      if (value) texts.push(value.slice(-500));
    });
  }
  return texts;
}
"""


def region_texts_say_no_more(texts: list[str]) -> bool:
    """只认列表容器里的结束语。整页其它区域的“暂无内容”不能结束当前列表。"""
    for text in texts:
        if "没有更多了" in text or "没有更多内容" in text:
            return True
    return False


def _page_says_no_more(page) -> bool:
    try:
        texts = page.evaluate(_LIST_END_SCRIPT, list(_LIST_ROOT_SELECTORS))
    except Exception:
        return False
    if not isinstance(texts, list):
        return False
    return region_texts_say_no_more([str(item) for item in texts])


def _polite_wait(page, settings: Settings) -> None:
    delay = max(300, settings.page_interval_ms) + random.randint(0, 400)
    page.wait_for_timeout(delay)


def _dismiss_obstructions(page) -> None:
    for selector in ("button.Modal-closeButton", "button[aria-label='关闭']"):
        try:
            locator = page.locator(selector)
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=1000)
        except Exception:
            continue


def _safe_content(page) -> str:
    try:
        return page.content()
    except Exception:
        return ""
