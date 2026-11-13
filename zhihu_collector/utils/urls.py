"""知乎链接识别。只解析公开页面 URL，不构造私有接口签名。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_HOSTS = {"zhihu.com", "www.zhihu.com", "m.zhihu.com"}

_USER_RE = re.compile(r"^/(people|org)/([^/?#]+)", re.IGNORECASE)
_ANSWER_RE = re.compile(r"^/question/(\d+)/answer/(\d+)/?$", re.IGNORECASE)
_QUESTION_RE = re.compile(r"^/question/(\d+)/?$", re.IGNORECASE)
_BARE_ANSWER_RE = re.compile(r"^/answer/(\d+)/?$", re.IGNORECASE)
_COLLECTION_RE = re.compile(r"^/collection/(\d+)/?$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedUrl:
    kind: str
    raw: str
    canonical: str
    user_token: str | None = None
    user_kind: str | None = None
    question_id: str | None = None
    answer_id: str | None = None
    collection_id: str | None = None

    @property
    def answers_page_url(self) -> str | None:
        if self.kind != "user" or not self.user_token or not self.user_kind:
            return None
        return f"https://www.zhihu.com/{self.user_kind}/{self.user_token}/answers"

    @property
    def pins_page_url(self) -> str | None:
        if self.kind != "user" or not self.user_token or not self.user_kind:
            return None
        return f"https://www.zhihu.com/{self.user_kind}/{self.user_token}/pins"

    @property
    def question_page_url(self) -> str | None:
        if not self.question_id:
            return None
        return f"https://www.zhihu.com/question/{self.question_id}"

    @property
    def answer_page_url(self) -> str | None:
        if self.kind != "answer" or not self.answer_id:
            return None
        if self.question_id:
            return f"https://www.zhihu.com/question/{self.question_id}/answer/{self.answer_id}"
        return f"https://www.zhihu.com/answer/{self.answer_id}"

    @property
    def collection_page_url(self) -> str | None:
        if self.kind != "collection" or not self.collection_id:
            return None
        return f"https://www.zhihu.com/collection/{self.collection_id}"


def is_zhihu_host(host: str | None) -> bool:
    hostname = (host or "").lower().rstrip(".")
    return hostname == "zhihu.com" or hostname.endswith(".zhihu.com")


def parse_zhihu_url(value: str) -> ParsedUrl:
    raw = (value or "").strip()
    if not raw:
        return ParsedUrl(kind="unknown", raw=value or "", canonical="")
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if host not in _HOSTS:
        return ParsedUrl(kind="unknown", raw=raw, canonical=candidate)
    path = (parsed.path or "/").strip()
    answer = _ANSWER_RE.match(path)
    if answer:
        question_id, answer_id = answer.group(1), answer.group(2)
        canonical = f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
        return ParsedUrl(
            kind="answer",
            raw=raw,
            canonical=canonical,
            question_id=question_id,
            answer_id=answer_id,
        )
    question = _QUESTION_RE.match(path)
    if question:
        question_id = question.group(1)
        canonical = f"https://www.zhihu.com/question/{question_id}"
        return ParsedUrl(
            kind="question",
            raw=raw,
            canonical=canonical,
            question_id=question_id,
        )
    bare = _BARE_ANSWER_RE.match(path)
    if bare:
        answer_id = bare.group(1)
        canonical = f"https://www.zhihu.com/answer/{answer_id}"
        return ParsedUrl(kind="answer", raw=raw, canonical=canonical, answer_id=answer_id)
    collection = _COLLECTION_RE.match(path)
    if collection:
        collection_id = collection.group(1)
        canonical = f"https://www.zhihu.com/collection/{collection_id}"
        return ParsedUrl(
            kind="collection",
            raw=raw,
            canonical=canonical,
            collection_id=collection_id,
        )
    user = _USER_RE.match(path)
    if user:
        user_kind = user.group(1).lower()
        token = user.group(2)
        canonical = f"https://www.zhihu.com/{user_kind}/{token}"
        return ParsedUrl(
            kind="user",
            raw=raw,
            canonical=canonical,
            user_token=token,
            user_kind=user_kind,
        )
    return ParsedUrl(kind="unknown", raw=raw, canonical=candidate)


def public_answer_url(question_id: str | None, answer_id: str | None) -> str:
    if question_id and answer_id:
        return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
    if answer_id:
        return f"https://www.zhihu.com/answer/{answer_id}"
    return ""


def public_question_url(question_id: str | None) -> str:
    if not question_id:
        return ""
    return f"https://www.zhihu.com/question/{question_id}"


def public_user_url(user_kind: str | None, token: str | None) -> str:
    if not token:
        return ""
    kind = user_kind or "people"
    return f"https://www.zhihu.com/{kind}/{token}"


def public_collection_url(collection_id: str | None) -> str:
    if not collection_id:
        return ""
    return f"https://www.zhihu.com/collection/{collection_id}"
