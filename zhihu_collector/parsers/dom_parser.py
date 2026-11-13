"""DOM 兜底解析。选择器来自 selectors.zhihu，不散落在采集流程里。"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from zhihu_collector.models import AuthorInfo, ContentRecord, dedupe_records
from zhihu_collector.parsers.images import extract_image_urls
from zhihu_collector.selectors.zhihu import ZhihuSelectors
from zhihu_collector.utils.urls import parse_zhihu_url

_VOTE_RE = re.compile(r"(\d+)")


def parse_answer_cards_html(html: str) -> list[ContentRecord]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    selectors = ZhihuSelectors()
    records: list[ContentRecord] = []
    for card in soup.select(selectors.answer_card):
        link = card.select_one(selectors.card_link)
        href = link.get("href") if link else ""
        parsed = parse_zhihu_url(_absolute(href))
        if parsed.kind != "answer" or not parsed.answer_id:
            continue
        title_node = card.select_one(selectors.card_title)
        title = _clean(title_node.get_text(" ", strip=True) if title_node else "")
        author_node = card.select_one(selectors.card_author)
        content_node = card.select_one(selectors.card_content)
        content_html = content_node.decode_contents() if content_node else ""
        vote_node = card.select_one(selectors.card_vote)
        is_collapsed = bool(
            card.select(".is-collapsed, .ContentItem-more, button.ContentItem-more, .RichContent.is-collapsed")
            or ("is-collapsed" in card.get("class", []))
            or (content_node and "is-collapsed" in content_node.get("class", []))
        )
        if not is_collapsed:
            for el in card.find_all(["button", "a", "span"]):
                text = el.get_text()
                if "阅读全文" in text or "展开阅读全文" in text:
                    is_collapsed = True
                    break
        records.append(
            ContentRecord(
                zhihu_content_id=parsed.answer_id,
                content_type="answer",
                url=parsed.canonical,
                title=title,
                author=AuthorInfo(name=_clean(author_node.get_text(" ", strip=True) if author_node else "")),
                question_id=parsed.question_id or "",
                question_title=title,
                question_url=parsed.question_page_url or "",
                html_content=content_html.strip(),
                vote_count=_vote(vote_node.get_text(" ", strip=True) if vote_node else ""),
                image_urls=extract_image_urls(content_html),
                content_incomplete=(not content_html.strip()) or is_collapsed,
            )
        )
    return dedupe_records(records)


def parse_primary_content_html(html: str) -> str:
    """回答详情页上，取正文区域第一段足够长的 RichText。"""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for selector in (
        ".QuestionAnswer-content .RichText",
        ".AnswerItem .RichText",
        ".RichContent.is-collapsed .RichText",
        ".RichContent .RichText",
        ".RichText",
    ):
        for node in soup.select(selector):
            if not node.get_text(strip=True):
                continue
            return node.decode_contents().strip()
    return ""


def _absolute(href: str | None) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("/"):
        return f"https://www.zhihu.com{href}"
    return href


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _vote(text: str) -> int | None:
    match = _VOTE_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1))
