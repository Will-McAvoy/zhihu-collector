"""采集答主公开回答。第一阶段只取回答列表的第一页。"""

from __future__ import annotations

import time

from zhihu_collector.parsers.identity import answer_container
from zhihu_collector.browser.network_listener import NetworkListener
from zhihu_collector.browser.page_guard import inspect_page
from zhihu_collector.config import Settings
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.models import ContentRecord, dedupe_records
from zhihu_collector.parsers.answer_parser import parse_answer_payloads
from zhihu_collector.parsers.dom_parser import parse_answer_cards_html, parse_primary_content_html
from zhihu_collector.parsers.embedded_json import extract_embedded_payloads
from zhihu_collector.selectors.zhihu import ZhihuSelectors
from zhihu_collector.utils.logger import get_logger

logger = get_logger("user_answers")


class UserAnswerCollector:
    def __init__(self, session, settings: Settings | None = None, controller: TaskController | None = None):
        self.session = session
        self.settings = settings or Settings()
        self.controller = controller or TaskController()
        self.listener = NetworkListener()

    def collect_first_page(self, answers_url: str, *, user_token: str, fetch_missing_content: bool = True) -> list[ContentRecord]:
        self.controller.checkpoint()
        page = self.session.new_page()
        self.listener.attach(page)
        logger.info("打开答主回答页 %s", answers_url)
        page.goto(answers_url, wait_until="domcontentloaded")
        _dismiss_obstructions(page)
        self._wait_until_answers(page, user_token, timeout_ms=12_000)
        _expand_read_more(page)
        html = page.content()
        records = records_from_page_sources(
            payloads=self._payloads_for_user(user_token),
            html=html,
            user_token=user_token,
        )
        logger.info("回答页停留在 %s，当前解析 %s 条", page.url, len(records))
        if not records:
            preview = ""
            try:
                preview = " ".join((page.inner_text("body") or "").split())[:300]
            except Exception:
                preview = ""
            logger.info("未解析到回答，页面摘要：%s", preview)
        inspect_page(page, found_content=len(records), network_errors=self.listener.errors)
        if fetch_missing_content:
            records = self._fill_missing_content(page, records)
        logger.info("第一页解析到 %s 条回答", len(records))
        return records

    def _payloads_for_user(self, user_token: str) -> list:
        captured = self.listener.snapshot()
        specific = [
            item.data
            for item in captured
            if f"/members/{user_token}/answers" in item.url and item.status < 400
        ]
        if specific:
            return specific
        return [
            item.data
            for item in captured
            if item.status < 400 and "/answers" in item.url
        ]

    def _wait_until_answers(self, page, user_token: str, timeout_ms: int) -> None:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            self.controller.checkpoint()
            if self._payloads_for_user(user_token):
                page.wait_for_timeout(600)
                return
            page.wait_for_timeout(300)

    def _fill_missing_content(self, page, records: list[ContentRecord]) -> list[ContentRecord]:
        interval = max(500, self.settings.request_interval_ms)
        for record in records:
            if record.html_content and not record.content_incomplete:
                continue
            if not record.url:
                continue
            self.controller.checkpoint()
            logger.info("列表中没有完整正文，打开回答页 %s", record.url)
            page.wait_for_timeout(interval)
            self.listener.clear()
            try:
                page.goto(record.url, wait_until="domcontentloaded")
            except Exception as exc:
                logger.warning("打开回答失败 %s：%s", record.url, exc)
                continue
            _dismiss_obstructions(page)
            self._wait_until_answers(page, record.author.url_token or "", timeout_ms=8_000)
            _expand_read_more(page)
            html = page.content()
            details = records_from_page_sources(
                payloads=self.listener.snapshot_data(),
                html=html,
                user_token=record.author.url_token,
            )
            matched = next(
                (item for item in details if item.zhihu_content_id == record.zhihu_content_id and item.html_content),
                None,
            )
            if matched is not None:
                record.html_content = matched.html_content
                record.image_urls = matched.image_urls or record.image_urls
                record.content_incomplete = matched.content_incomplete
                if matched.vote_count is not None:
                    record.vote_count = matched.vote_count
                continue
            if html:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "lxml")
                target_node = answer_container(soup, record.zhihu_content_id)
                if target_node:
                    rt = target_node.select_one(".RichContent .RichText, .RichText")
                    if rt and rt.decode_contents().strip():
                        if not target_node.select(".is-collapsed, .ContentItem-more"):
                            record.html_content = rt.decode_contents().strip()
                            record.content_incomplete = False
        return records


def records_from_page_sources(*, payloads: list, html: str, user_token: str = "") -> list[ContentRecord]:
    from_json = parse_answer_payloads(payloads)
    from_embedded = parse_answer_payloads(extract_embedded_payloads(html))
    from_dom = parse_answer_cards_html(html)
    merged = dedupe_records([*from_json, *from_embedded, *from_dom])
    return _keep_author(merged, user_token)


def _keep_author(records: list[ContentRecord], user_token: str) -> list[ContentRecord]:
    if not user_token:
        return records
    known = [record for record in records if record.author.url_token]
    if not known:
        return records
    if not any(record.author.url_token == user_token for record in known):
        return records
    return [
        record
        for record in records
        if not record.author.url_token or record.author.url_token == user_token
    ]


def _dismiss_obstructions(page) -> None:
    for selector in ("button.Modal-closeButton", "button[aria-label='关闭']"):
        try:
            locator = page.locator(selector)
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=1000)
        except Exception:
            continue


def _expand_read_more(page) -> None:
    try:
        locator = page.locator(ZhihuSelectors.read_more)
        count = min(locator.count(), 30)
    except Exception:
        return
    for index in range(count):
        try:
            locator.nth(index).click(timeout=800)
        except Exception:
            continue
