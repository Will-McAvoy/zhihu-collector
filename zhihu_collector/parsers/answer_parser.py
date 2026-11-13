"""从知乎页面自己返回的 JSON 中提取回答。"""

from __future__ import annotations

from typing import Any

from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.parsers.images import extract_image_urls
from zhihu_collector.utils.time_utils import normalize_unix
from zhihu_collector.utils.urls import public_answer_url, public_question_url, public_user_url


def parse_answer_payloads(payloads: list[Any]) -> list[ContentRecord]:
    records: list[ContentRecord] = []
    for payload in payloads:
        records.extend(_walk(payload))
    return records


def parse_answer_object(data: dict) -> ContentRecord | None:
    if data.get("type") not in (None, "answer", "article"):
        return None
    # 文章不是本阶段的目标。没有问题信息的对象也不当作回答。
    if data.get("type") == "article":
        return None
    answer_id = _text_id(data.get("id"))
    question = data.get("question") if isinstance(data.get("question"), dict) else {}
    question_id = _text_id(question.get("id") or data.get("question_id"))
    if not answer_id:
        return None
    if not question and data.get("type") != "answer":
        return None
    html = _html_from(data)
    excerpt = _text(data.get("excerpt") or data.get("excerpt_new"))
    author = _author(data.get("author"))
    title = _text(question.get("title") or data.get("title"))
    record = ContentRecord(
        zhihu_content_id=answer_id,
        content_type="answer",
        url=public_answer_url(question_id, answer_id),
        title=title,
        author=author,
        question_id=question_id,
        question_title=title,
        question_url=public_question_url(question_id),
        html_content=html,
        excerpt=excerpt,
        created_time=normalize_unix(data.get("created_time") or data.get("created")),
        updated_time=normalize_unix(data.get("updated_time") or data.get("updated")),
        vote_count=_optional_int(data.get("voteup_count") if "voteup_count" in data else data.get("vote_count")),
        comment_count=_optional_int(data.get("comment_count")),
        image_urls=extract_image_urls(html),
        content_incomplete=not html or bool(data.get("content_need_truncated") or data.get("is_jump_native")),
    )
    return record


def _walk(node: Any) -> list[ContentRecord]:
    found: list[ContentRecord] = []
    if isinstance(node, dict):
        unwrapped = _unwrap(node)
        if unwrapped is not None:
            parsed = parse_answer_object(unwrapped)
            if parsed is not None:
                found.append(parsed)
        for value in node.values():
            found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def _unwrap(data: dict) -> dict | None:
    if _looks_like_answer(data):
        return data
    for key in ("target", "object", "answer"):
        inner = data.get(key)
        if isinstance(inner, dict) and _looks_like_answer(inner):
            return inner
    return None


def _looks_like_answer(data: dict) -> bool:
    if data.get("type") == "answer" and data.get("id") is not None:
        return True
    question = data.get("question")
    return isinstance(question, dict) and data.get("id") is not None and (
        "content" in data or "excerpt" in data or "voteup_count" in data
    )


def _author(raw: Any) -> AuthorInfo:
    if not isinstance(raw, dict):
        return AuthorInfo(name="匿名用户")
    token = _text(raw.get("url_token"))
    user_kind = "org" if raw.get("is_org") else "people"
    name = _text(raw.get("name")) or "匿名用户"
    return AuthorInfo(
        zhihu_user_id=_text_id(raw.get("id")),
        name=name,
        headline=_text(raw.get("headline")),
        url_token=token,
        url=public_user_url(user_kind, token) if token else "",
        avatar_url=_text(raw.get("avatar_url") or raw.get("avatar_url_template")),
    )


def _html_from(data: dict) -> str:
    for key in ("content", "editable_content", "rich_text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _text_id(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value).strip()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
