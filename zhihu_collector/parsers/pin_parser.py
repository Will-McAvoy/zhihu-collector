"""从知乎想法 JSON 提取统一内容记录。"""

from __future__ import annotations

import html
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from zhihu_collector.models import ContentRecord
from zhihu_collector.parsers.answer_parser import _author, _optional_int, _text, _text_id
from zhihu_collector.parsers.images import extract_image_urls
from zhihu_collector.utils.time_utils import normalize_unix
from zhihu_collector.parsers.video_refs import html_has_video_reference


def parse_pin_payloads(payloads: list[Any]) -> list[ContentRecord]:
    records: list[ContentRecord] = []
    for payload in payloads:
        records.extend(_walk(payload))
    return records


def parse_pin_object(data: dict) -> ContentRecord | None:
    if not _looks_like_pin(data):
        return None
    pin_id = _text_id(data.get("id"))
    if not pin_id:
        return None
    body_html, image_urls = _pin_html(data)
    excerpt = _text(data.get("excerpt") or data.get("excerpt_title"))
    if "<" in excerpt or "&" in excerpt:
        excerpt = BeautifulSoup(excerpt, "lxml").get_text(" ", strip=True)
    if not excerpt and body_html:
        excerpt = BeautifulSoup(body_html, "lxml").get_text(" ", strip=True)[:100]
    author = _author(data.get("author"))
    return ContentRecord(
        zhihu_content_id=pin_id,
        content_type="pin",
        url=_pin_url(data, pin_id, author.url_token),
        title=excerpt[:40],
        author=author,
        html_content=body_html,
        excerpt=excerpt,
        created_time=normalize_unix(data.get("created") or data.get("created_time")),
        updated_time=normalize_unix(data.get("updated") or data.get("updated_time")),
        vote_count=_optional_int(
            data.get("like_count")
            if data.get("like_count") is not None
            else data.get("reaction_count")
        ),
        comment_count=_optional_int(data.get("comment_count")),
        image_urls=image_urls or extract_image_urls(body_html),
        content_incomplete=not body_html,
    )


def _walk(node: Any) -> list[ContentRecord]:
    found: list[ContentRecord] = []
    if isinstance(node, dict):
        target = node.get("target") if isinstance(node.get("target"), dict) else None
        for candidate in (node, target):
            if isinstance(candidate, dict) and _looks_like_pin(candidate):
                parsed = parse_pin_object(candidate)
                if parsed is not None:
                    found.append(parsed)
        for value in node.values():
            if isinstance(value, (dict, list)):
                found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def _looks_like_pin(data: dict) -> bool:
    if data.get("type") == "pin" and data.get("id") is not None:
        return True
    return False


def _pin_url(data: dict, pin_id: str, token: str) -> str:
    raw = data.get("url")
    if isinstance(raw, str) and raw.startswith("https://www.zhihu.com/pin/"):
        return raw.split("?", 1)[0]
    if token:
        return f"https://www.zhihu.com/pin/{pin_id}"
    return f"https://www.zhihu.com/pin/{pin_id}"


def _usable_html(markup: str) -> bool:
    return bool(markup.strip() and (
        extract_image_urls(markup)
        or html_has_video_reference(markup)
        or BeautifulSoup(markup, "lxml").get_text(" ", strip=True)
    ))


def _pin_html(data: dict) -> tuple[str, list[str]]:
    content = data.get("content")
    for value in (data.get("content_html"), data.get("own_text_html")):
        if isinstance(value, str) and _usable_html(value):
            # HTML 里有正文时也保留仅出现在结构化数据中的链接卡片。
            if isinstance(content, list):
                existing_links = {
                    _safe_web_url(link.get("href"))
                    for link in BeautifulSoup(value, "lxml").select("a[href]")
                }
                cards = [
                    block for block in content
                    if isinstance(block, dict) and block.get("type") == "link_card"
                    and _card_url(block) not in existing_links
                ]
                more, _ = _blocks_to_html(cards)
                if more:
                    value = f"{value}\n{more}"
            return value, extract_image_urls(value)
    if isinstance(content, str) and content.strip():
        if "<" not in content:
            return f"<p>{html.escape(content)}</p>", []
        if _usable_html(content):
            return content, extract_image_urls(content)
    if isinstance(content, list):
        body, images = _blocks_to_html(content)
        if body:
            return body, images
    text = _text(data.get("content_text"))
    if text:
        return f"<p>{html.escape(text)}</p>", []
    return "", []


def _safe_web_url(value: Any) -> str:
    raw = value.strip() if isinstance(value, str) else ""
    if not raw or any(char.isspace() or ord(char) < 32 for char in raw):
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        url = urlsplit(raw)
        if url.scheme in {"https", "http"} and url.hostname and not (url.username or url.password):
            return raw
    except ValueError:
        pass
    return ""


def _card_url(block: dict) -> str:
    return _safe_web_url(block.get("url"))


def _blocks_to_html(blocks: list) -> tuple[str, list[str]]:
    parts: list[str] = []
    images: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind in {"text", "paragraph"}:
            text = _text(block.get("content") or block.get("own_text") or block.get("text"))
            if text:
                parts.append(f"<p>{html.escape(text)}</p>")
        elif kind in {"image", "video"}:
            url = _text(
                block.get("url")
                or block.get("original_url")
                or (block.get("playlist") if isinstance(block.get("playlist"), str) else "")
            )
            thumbnail = _text(block.get("thumbnail") or block.get("cover"))
            image_url = url if kind == "image" else thumbnail
            if image_url.startswith("http"):
                images.append(image_url)
                parts.append(f'<p><img src="{html.escape(image_url, quote=True)}"/></p>')
            if kind == "video" and url.startswith("http"):
                parts.append(f'<p><a href="{html.escape(url, quote=True)}">视频</a></p>')
        elif kind in {"link", "link_card"}:
            href = _card_url(block)
            title = _text(block.get("data_draft_title") or block.get("title") or href)
            if href:
                parts.append(f'<p><a href="{html.escape(href, quote=True)}">{html.escape(title)}</a></p>')
    return "\n".join(parts), images
