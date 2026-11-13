"""知乎收藏夹条目解析与标题提取。

只提取回答类型的条目，非回答条目独立计数，不与回答失败混淆。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from bs4 import BeautifulSoup
from urllib.parse import parse_qs, urlparse

from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.parsers.images import extract_image_urls
from zhihu_collector.utils.urls import public_answer_url, public_question_url, public_user_url


_NON_ANSWER_TYPES = {"article", "zvideo", "video", "pin", "column", "question", "commercial_answer"}
_COLLECTION_QUERY_KEYS = {"collection_id", "collectionid", "favlist_id", "favlistid"}
_BODY_OR_RECOMMEND = re.compile(
    r"RichText|RichContent|Recommendations|RelatedReadings|ContentItem-actions|CommentList"
)
_ANSWER_PATH = re.compile(r"^/question/(\d+)/answer/(\d+)/?$")


@dataclass
class ParsedCollectionItems:
    answers: list[ContentRecord]
    non_answer_count: int = 0
    unknown_type_count: int = 0
    duplicate_count: int = 0
    non_answer_keys: list[str] = field(default_factory=list)
    unknown_keys: list[str] = field(default_factory=list)
    duplicate_keys: list[str] = field(default_factory=list)


def _zhihu_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "zhihu.com" or host.endswith(".zhihu.com")


def collection_identity_from_url(url: str) -> tuple[str, str]:
    """识别 URL 是否为明确的收藏夹端点。

    返回 (kind, collection_id)。kind 为 foreign、collection 或 other。
    问题回答、推荐流等端点即使路径里出现相同数字，也是 other。
    """
    if not url or not _zhihu_host(url):
        return "foreign", ""
    parsed = urlparse(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    for index, segment in enumerate(segments):
        if segment in {"collections", "favlists", "collection"} and index + 1 < len(segments):
            candidate = segments[index + 1]
            if candidate.isdigit():
                return "collection", candidate
    for key, values in parse_qs(parsed.query, keep_blank_values=False).items():
        if key.lower() in _COLLECTION_QUERY_KEYS and values:
            value = str(values[0]).strip()
            if value.isdigit():
                return "collection", value
    return "other", ""


def collection_identity_from_payload(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("collection", "favlist"):
        holder = payload.get(key)
        if isinstance(holder, dict):
            value = str(holder.get("id") or "").strip()
            if value:
                return value
    item_type = str(payload.get("type") or "").lower()
    if item_type in {"collection", "favlist"}:
        return str(payload.get("id") or "").strip()
    return ""


def payload_matches_collection(payload: dict, url: str, target_collection_id: str) -> bool:
    """只接受 URL 与 payload 同时指向同一目标收藏夹的响应。

    任一条件碰巧命中不够。两边都给出身份且不一致时拒绝。
    问题回答、其他域名或其他收藏夹都不能混入。
    """
    target = str(target_collection_id or "").strip()
    if not target or not isinstance(payload, dict):
        return False
    kind, url_id = collection_identity_from_url(url)
    if kind != "collection" or url_id != target:
        return False
    payload_id = collection_identity_from_payload(payload)
    if payload_id and payload_id != url_id:
        return False
    return True


def is_collection_list_payload(url: str, payload: dict) -> bool:
    """分页结束证据只来自收藏夹列表响应，不来自标题元数据。"""
    if not isinstance(payload, dict):
        return False
    path = urlparse(url).path
    if "/items" in path or path.rstrip("/").endswith("/contents"):
        return True
    if not isinstance(payload.get("data"), list):
        return False
    return "/collections/" in path or "/favlists/" in path


def _looks_truncated(html_content: str) -> bool:
    if not html_content or not html_content.strip():
        return True
    soup = BeautifulSoup(html_content, "lxml")
    if soup.select(".is-collapsed, .ContentItem-more, .RichContent-inner--collapsed"):
        return True
    if any(node.get_text(" ", strip=True) in {"阅读全文", "展开全文"}
           for node in soup.select("button, [role='button']")):
        return True
    return not soup.get_text(" ", strip=True) and not soup.select_one("img, video, figure")


def answer_body_needs_enrichment(html_content: str) -> bool:
    """显式不完整或带折叠标记的正文需要补全。短回答和纯图片回答不是摘要。"""
    return _looks_truncated(html_content)


def collection_dom_end_state(html: str) -> str:
    """只有收藏夹列表容器自身的结束标记才算结束。全页任意“暂无内容”不算。"""
    if not html or not html.strip():
        return "unknown"
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one(".CollectionDetailPage-main, .CollectionDetailPage")
    if container is None:
        return "unknown"
    empty = container.select_one(".List-empty")
    if empty is not None and empty.get_text(" ", strip=True):
        return "ended"
    for node in container.find_all(string=re.compile(r"没有更多内容了")):
        parent = getattr(node, "parent", None)
        if parent is None or _inside_body_or_recommend(parent):
            continue
        return "ended"
    return "unknown"


def _inside_body_or_recommend(node) -> bool:
    current = node
    while current is not None and getattr(current, "name", None):
        classes = " ".join(current.get("class") or [])
        module = str(current.get("data-za-detail-view-path-module") or "")
        if _BODY_OR_RECOMMEND.search(classes) or module in {"RightSideBar", "Recommendation", "Related"}:
            return True
        current = current.parent
    return False


def _precise_answer_target(href: str) -> tuple[str, str] | None:
    cleaned = (href or "").strip()
    if not cleaned or cleaned.lower().startswith(("javascript:", "data:")):
        return None
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    parsed = urlparse(cleaned)
    if parsed.netloc and not _zhihu_host(cleaned):
        return None
    path = parsed.path or ""
    if not path.startswith("/"):
        path = "/" + path
    match = _ANSWER_PATH.fullmatch(path)
    if match is None:
        return None
    return match.group(1), match.group(2)


def extract_collection_title(payloads: list[dict], html: str, fallback_id: str = "") -> str:
    # 优先从接口数据中提取收藏夹标题
    for payload in payloads:
        if isinstance(payload, dict):
            for key in ("collection", "favlist"):
                coll = payload.get(key)
                if isinstance(coll, dict) and coll.get("title"):
                    return str(coll["title"]).strip()
            item_type = str(payload.get("type") or "").lower()
            if item_type in {"collection", "favlist"} and payload.get("title"):
                return str(payload["title"]).strip()

    # 从 DOM HTML 提取
    if html and html.strip():
        soup = BeautifulSoup(html, "lxml")
        title_node = soup.select_one(
            "h1.CollectionDetailPageHeader-title, .CollectionHeader-title, h1, .CollectionDetailPageHeader-titleBlock"
        )
        if title_node and title_node.get_text(strip=True):
            return title_node.get_text(strip=True)

        if soup.title and soup.title.string:
            clean_title = re.sub(r"^\s*[(（]\s*\d+\s*(?:封私信|条(?:新)?消息)[^）)]*[)）]\s*", "", soup.title.string)
            clean_title = re.sub(r"[-_|｜]\s*知乎.*$", "", clean_title).strip()
            if clean_title:
                return clean_title

    return f"收藏夹-{fallback_id}" if fallback_id else "未命名收藏夹"


def parse_collection_items(
    payloads: list[dict],
    html: str = "",
    target_collection_id: str = "",
) -> tuple[list[ContentRecord], int, int]:
    """兼容旧接口：返回回答、非回答数和重复数。"""
    result = parse_collection_items_detailed(payloads, html, target_collection_id)
    return result.answers, result.non_answer_count, result.duplicate_count


def parse_collection_items_detailed(
    payloads: list[dict],
    html: str = "",
    target_collection_id: str = "",
) -> ParsedCollectionItems:
    """解析收藏夹条目，并把无法识别的条目单独计数。"""
    answers: list[ContentRecord] = []
    seen_ids: set[str] = set()
    non_answer_keys: list[str] = []
    unknown_keys: list[str] = []
    duplicate_keys: list[str] = []

    def _remember_unknown(key: str) -> None:
        if key not in unknown_keys:
            unknown_keys.append(key)

    def _remember_non_answer(key: str) -> None:
        if key not in non_answer_keys:
            non_answer_keys.append(key)

    # 1. 优先解析网络 JSON 响应
    for payload_index, payload in enumerate(payloads):
        if not isinstance(payload, dict):
            continue

        items = payload.get("data")
        if not isinstance(items, list):
            continue

        for item_index, raw_item in enumerate(items):
            if not isinstance(raw_item, dict):
                _remember_unknown(f"payload-{payload_index}-item-{item_index}")
                continue

            # Zhihu collection items wrap data under 'content'
            content = raw_item.get("content") if isinstance(raw_item.get("content"), dict) else raw_item
            item_type = str(content.get("type") or raw_item.get("type") or "").lower()
            raw_id = str(content.get("id") or raw_item.get("id") or item_index).strip()

            if not item_type:
                _remember_unknown(f"unknown:{payload_index}:{raw_id}")
                continue
            if item_type != "answer" and item_type in _NON_ANSWER_TYPES:
                _remember_non_answer(f"{item_type}:{raw_id}")
                continue
            if item_type != "answer":
                _remember_unknown(f"{item_type}:{raw_id}")
                continue

            answer_id = str(content.get("id") or "").strip()
            if not answer_id:
                _remember_unknown(f"answer-missing-id:{payload_index}:{item_index}")
                continue

            if answer_id in seen_ids:
                if answer_id not in duplicate_keys:
                    duplicate_keys.append(answer_id)
                continue
            seen_ids.add(answer_id)

            # 提取回答详细信息
            question_data = content.get("question") if isinstance(content.get("question"), dict) else {}
            question_id = str(question_data.get("id") or "").strip()
            question_title = str(question_data.get("title") or "").strip()

            author_data = content.get("author") if isinstance(content.get("author"), dict) else {}
            author = AuthorInfo(
                zhihu_user_id=str(author_data.get("id") or ""),
                name=str(author_data.get("name") or "知乎用户"),
                headline=str(author_data.get("headline") or ""),
                url_token=str(author_data.get("url_token") or ""),
                url=public_user_url(author_data.get("user_type"), author_data.get("url_token")),
                avatar_url=str(author_data.get("avatar_url") or ""),
            )

            html_content = content.get("content") or ""
            excerpt = content.get("excerpt") or ""

            # 判断内容是否为不完整摘要：如果只有 excerpt 或 html_content 带明确折叠/展开标记
            is_incomplete = _looks_truncated(html_content)

            url = content.get("url") or public_answer_url(question_id, answer_id)
            if "api.zhihu.com" in url or "/api/v4/" in url:
                url = public_answer_url(question_id, answer_id)

            record = ContentRecord(
                zhihu_content_id=answer_id,
                content_type="answer",
                url=url,
                title=question_title or f"回答-{answer_id}",
                author=author,
                question_id=question_id,
                question_title=question_title,
                question_url=public_question_url(question_id),
                html_content=html_content,
                excerpt=excerpt,
                created_time=content.get("created_time"),
                updated_time=content.get("updated_time"),
                vote_count=content.get("voteup_count"),
                comment_count=content.get("comment_count"),
                image_urls=extract_image_urls(html_content) if html_content else [],
                content_incomplete=is_incomplete,
            )
            answers.append(record)

    # 2. 如果网络 JSON 未发现回答，使用收藏夹列表容器内的 DOM 兜底。
    # 正文引用和推荐区里的回答链接不是收藏夹成员。
    if not answers and html and html.strip():
        soup = BeautifulSoup(html, "lxml")
        container = soup.select_one(".CollectionDetailPage-main, .CollectionDetailPage, [data-za-detail-view-path-module='CollectionDetail']")
        if container is None:
            return _parsed_items(answers, non_answer_keys, unknown_keys, duplicate_keys)
        cards = container.select(".List-item, .ContentItem, .CollectionItem")
        for card_index, card in enumerate(cards):
            if _inside_body_or_recommend(card):
                continue
            target = None
            for link in card.select("a[href]"):
                if _inside_body_or_recommend(link):
                    continue
                target = _precise_answer_target(link.get("href") or "")
                if target is not None:
                    break
            if target is None:
                other = card.select_one("a[href*='/p/'], a[href*='/pin/'], a[href*='/zvideo/']")
                if other is not None and not _inside_body_or_recommend(other):
                    _remember_non_answer(f"dom-non-answer:{other.get('href')}")
                else:
                    _remember_unknown(f"dom-unknown:{card_index}")
                continue

            question_id, answer_id = target
            if answer_id in seen_ids:
                if answer_id not in duplicate_keys:
                    duplicate_keys.append(answer_id)
                continue
            seen_ids.add(answer_id)

            title_elem = card.select_one("h2, .ContentItem-title")
            question_title = title_elem.get_text(strip=True) if title_elem else ""
            author_elem = card.select_one(".AuthorInfo-name, .UserLink-link")
            author_name = author_elem.get_text(strip=True) if author_elem else "知乎用户"
            content_elem = card.select_one(".RichContent .RichText, .RichText")
            content_html = content_elem.decode_contents().strip() if content_elem else ""
            record = ContentRecord(
                zhihu_content_id=answer_id,
                content_type="answer",
                url=public_answer_url(question_id, answer_id),
                title=question_title or f"回答-{answer_id}",
                author=AuthorInfo(name=author_name),
                question_id=question_id,
                question_title=question_title,
                question_url=public_question_url(question_id),
                html_content=content_html,
                excerpt="",
                image_urls=extract_image_urls(content_html) if content_html else [],
                content_incomplete=True,
            )
            answers.append(record)

    return _parsed_items(answers, non_answer_keys, unknown_keys, duplicate_keys)


def _parsed_items(answers, non_answer_keys, unknown_keys, duplicate_keys) -> ParsedCollectionItems:
    return ParsedCollectionItems(
        answers=answers,
        non_answer_count=len(non_answer_keys),
        unknown_type_count=len(unknown_keys),
        duplicate_count=len(duplicate_keys),
        non_answer_keys=list(non_answer_keys),
        unknown_keys=list(unknown_keys),
        duplicate_keys=list(duplicate_keys),
    )
