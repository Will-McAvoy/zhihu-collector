"""遍历和列举知乎收藏夹中的全部回答。

包含网络监听过滤、DOM兜底、非回答统计、去重与诊断记录。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from zhihu_collector.browser.network_listener import NetworkListener
from zhihu_collector.browser.page_guard import inspect_page
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.listing import _goto
from zhihu_collector.config import Settings
from zhihu_collector.errors import LoginExpiredError, TaskStopped, VerificationRequiredError
from zhihu_collector.models import ContentRecord
from zhihu_collector.parsers.collection_parser import (
    collection_dom_end_state,
    collection_identity_from_url,
    extract_collection_title,
    is_collection_list_payload,
    parse_collection_items_detailed,
    payload_matches_collection,
)
from zhihu_collector.utils.logger import get_logger
from zhihu_collector.utils.retry import RetryGaveUp
from zhihu_collector.utils.urls import public_collection_url

logger = get_logger("collection_answers")


def _advance_page(page, listener, controller, settings):
    """Use the site's pagination control; infinite scrolling is only a fallback."""
    controller.checkpoint()
    buttons = page.locator('.Pagination button:has-text("下一页"), .Pagination a:has-text("下一页")')
    count = buttons.count()
    if isinstance(count, int) and count > 0:
        button = buttons.first
        if button.is_visible() is True and button.is_enabled() is True:
            button.click(timeout=5000)
            # Pump completed network events before the next parse; never reuse old payloads.
            for _ in range(30):
                controller.checkpoint()
                page.wait_for_timeout(200)
                if listener.has_listing_payload:
                    return
            return
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    # Cancellable pacing, unlike a single time.sleep during a long task.
    for _ in range(max(1, settings.page_interval_ms // 200)):
        controller.checkpoint()
        page.wait_for_timeout(200)


@dataclass
class CollectionManifest:
    collection_id: str
    title: str
    url: str
    answers: list[ContentRecord] = field(default_factory=list)
    non_answer_count: int = 0
    unknown_type_count: int = 0
    duplicate_count: int = 0
    total_declared: int = 0
    integrity_status: str = "unknown"  # 'complete' | 'partial' | 'empty'
    finish_reason: str = ""
    pages_traversed: int = 0
    page_summaries: list[dict] = field(default_factory=list)
    cursor_history: list[str] = field(default_factory=list)
    unprocessed_count: int = 0
    snapshot_id: int | None = None
    interrupted: str = ""
    reached_limit: bool = False


def _confirmed_paging(items, collection_id: str) -> dict | None:
    """只取已经确认属于当前收藏夹列表的分页，忽略其他接口的 last_paging。"""
    confirmed = None
    for item in items:
        data = getattr(item, "data", None)
        url = getattr(item, "url", "") or ""
        status = getattr(item, "status", 0)
        if not 200 <= status < 300 or not isinstance(data, dict) or not isinstance(data.get("data"), list):
            continue
        if not payload_matches_collection(data, url, collection_id):
            continue
        if not is_collection_list_payload(url, data):
            continue
        raw = data.get("paging")
        if not isinstance(raw, dict):
            continue
        confirmed = {
            "is_end": raw.get("is_end") is True,
            "end_known": "is_end" in raw,
            "totals": raw.get("totals"),
            "next": str(raw.get("next") or ""),
            "cursor": str(raw.get("next") or raw.get("previous") or ""),
        }
    return confirmed


def collect_collection_manifest(
    session,
    collection_id: str,
    settings: Settings,
    controller: TaskController,
    on_progress: Callable[..., None] | None = None,
    on_log: Callable[[str], None] | None = None,
    max_items: int = 0,
) -> CollectionManifest:
    """读取指定收藏夹的全部内容清单，仅发现条目，不写正式导出文件。"""
    on_progress = on_progress or (lambda *args, **kwargs: None)
    on_log = on_log or (lambda msg: None)

    collection_url = public_collection_url(collection_id)
    on_log(f"开始读取收藏夹清单：{collection_url}")

    # 每次调用使用独立列表。不能借用模块级可变对象在多次任务之间补状态。
    page_summaries: list[dict] = []
    cursor_history: list[str] = []
    answers: list[ContentRecord] = []
    seen_ids: set[str] = set()
    seen_non_answers: set[str] = set()
    seen_unknown: set[str] = set()
    seen_duplicates: set[str] = set()
    seen_cursors: set[str] = set()
    total_declared = 0
    pages_traversed = 0
    stale_rounds = 0
    finish_reason = ""
    integrity_status = "unknown"
    interrupted = ""
    reached_limit = False
    title = f"收藏夹-{collection_id}"
    page = None

    try:
        listener = NetworkListener()
        page = session.new_page()
        listener.attach(page)
        controller.checkpoint()
        try:
            _goto(page, collection_url, settings, controller, timeout_ms=30_000, transient_only=True)
        except RetryGaveUp as exc:
            original = exc.last_error.__cause__
            if not isinstance(original, PlaywrightTimeout) or not _confirmed_paging(listener.snapshot(), collection_id):
                raise
            logger.warning("收藏夹导航超时，但已取得本收藏夹的有效列表，继续处理")

        page.wait_for_timeout(max(500, settings.request_interval_ms))
        initial_payloads = [
            item.data
            for item in listener.snapshot()
            if item.status < 400
            and isinstance(item.data, dict)
            and payload_matches_collection(item.data, item.url, collection_id)
        ]
        initial_html = page.content()
        title = extract_collection_title(initial_payloads, initial_html, fallback_id=collection_id)
        on_log(f"识别收藏夹：{title} (ID: {collection_id})")

        while True:
            controller.checkpoint()
            pages_traversed += 1
            raw_items = listener.consume()
            filtered_items = []
            filtered_payloads = []
            for item in raw_items:
                if (
                    item.status < 400
                    and isinstance(item.data, dict)
                    and payload_matches_collection(item.data, item.url, collection_id)
                ):
                    filtered_items.append(item)
                    filtered_payloads.append(item.data)

            current_html = page.content() if not filtered_payloads else ""
            parsed = parse_collection_items_detailed(
                filtered_payloads,
                current_html,
                target_collection_id=collection_id,
            )
            known_other = len(seen_non_answers) + len(seen_unknown)
            for key in parsed.non_answer_keys:
                seen_non_answers.add(key)
            for key in parsed.unknown_keys:
                seen_unknown.add(key)
            for key in parsed.duplicate_keys:
                seen_duplicates.add(key)

            paging = _confirmed_paging(filtered_items, collection_id)
            if paging and paging.get("totals") and not total_declared:
                try:
                    total_declared = int(paging["totals"])
                except (ValueError, TypeError):
                    pass

            collection_errors = [
                item for item in listener.consume_errors()
                if collection_identity_from_url(str(item.get("url") or "")) == ("collection", collection_id)
            ]
            inspect_page(page, found_content=len(answers) + len(parsed.answers), network_errors=collection_errors)

            fresh_added = 0
            for ans in parsed.answers:
                if ans.zhihu_content_id not in seen_ids:
                    seen_ids.add(ans.zhihu_content_id)
                    answers.append(ans)
                    fresh_added += 1
                else:
                    seen_duplicates.add(ans.zhihu_content_id)

            page_cursor = str(paging.get("cursor") or "") if paging else ""
            end_known = bool(paging and paging.get("end_known"))
            page_summaries.append({
                "page": pages_traversed,
                "new_answers": fresh_added,
                "filtered_payloads": len(filtered_payloads),
                "cursor": page_cursor[:120],
                "is_end": paging.get("is_end") if end_known else None,
                "paging_bound": paging is not None,
            })
            on_log(
                f"第 {pages_traversed} 页：新增 {fresh_added} 篇回答，累计 {len(answers)} 篇回答"
                + (f"（已过滤非回答 {len(seen_non_answers)} 条）" if seen_non_answers else "")
            )
            on_progress(len(answers), total_declared or len(answers), f"已读取 {len(answers)} 篇")

            if max_items > 0 and len(answers) >= max_items:
                finish_reason = f"达到设定的篇数上限 {max_items} 篇"
                integrity_status = "partial"
                answers = answers[:max_items]
                reached_limit = True
                break

            # 知乎末页也可能带一个残留 next；明确的末页信号优先于游标检查。
            confirmed_end = bool(paging and paging.get("is_end"))
            dom_end = paging is None and collection_dom_end_state(current_html or page.content()) == "ended"
            if collection_errors and not filtered_payloads:
                confirmed_end = False
                dom_end = False
            if confirmed_end or dom_end:
                observed = len(seen_ids) + len(seen_non_answers) + len(seen_unknown)
                if total_declared > 0 and total_declared != observed:
                    finish_reason = f"已到达收藏夹末尾，但参考 {total_declared} 条，识别 {observed} 条，完整性未确认"
                    integrity_status = "partial" if answers else "unknown"
                else:
                    finish_reason = "已到达收藏夹末尾"
                    integrity_status = "complete" if answers else "empty"
                break

            if page_cursor:
                if page_cursor in seen_cursors and filtered_payloads:
                    finish_reason = "检测到重复游标，已停止读取以避免重复页面"
                    integrity_status = "partial" if answers else "unknown"
                    break
                seen_cursors.add(page_cursor)
                cursor_history.append(page_cursor[:120])

            if fresh_added == 0 and len(seen_non_answers) + len(seen_unknown) == known_other:
                stale_rounds += 1
                if stale_rounds >= max(3, settings.max_stale_rounds):
                    if collection_errors:
                        finish_reason = "收藏夹列表请求失败，且没有可靠的结束证据"
                    else:
                        finish_reason = f"连续 {stale_rounds} 轮滚动未发现新条目，停止读取"
                    integrity_status = "partial" if answers else "unknown"
                    break
            else:
                stale_rounds = 0

            controller.checkpoint()
            _advance_page(page, listener, controller, settings)
    except TaskStopped:
        interrupted = "stopped"
        finish_reason = finish_reason or "用户取消，已保留已发现清单"
        if integrity_status in {"", "unknown", "complete"}:
            integrity_status = "partial" if answers else "unknown"
    except LoginExpiredError as exc:
        interrupted = "login"
        finish_reason = str(exc)
        integrity_status = "partial" if answers else "unknown"
    except VerificationRequiredError as exc:
        interrupted = "verification"
        finish_reason = str(exc)
        integrity_status = "partial" if answers else "unknown"
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    return CollectionManifest(
        collection_id=collection_id,
        title=title,
        url=collection_url,
        answers=answers,
        non_answer_count=len(seen_non_answers),
        unknown_type_count=len(seen_unknown),
        duplicate_count=len(seen_duplicates),
        total_declared=total_declared,
        integrity_status=integrity_status,
        finish_reason=finish_reason,
        pages_traversed=pages_traversed,
        page_summaries=page_summaries,
        cursor_history=cursor_history,
        interrupted=interrupted,
        reached_limit=reached_limit,
    )
