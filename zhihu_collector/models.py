"""采集结果的内存模型。数据库和导出都使用这份结构。"""

from __future__ import annotations

from dataclasses import dataclass, field

from zhihu_collector.utils.hashing import sha256_text
from zhihu_collector.utils.time_utils import unix_to_iso


@dataclass
class AuthorInfo:
    zhihu_user_id: str = ""
    name: str = ""
    headline: str = ""
    url_token: str = ""
    url: str = ""
    avatar_url: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.zhihu_user_id,
            "name": self.name,
            "headline": self.headline,
            "url_token": self.url_token,
            "url": self.url,
            "avatar_url": self.avatar_url,
        }


@dataclass
class ContentRecord:
    zhihu_content_id: str
    content_type: str
    url: str
    title: str = ""
    author: AuthorInfo = field(default_factory=AuthorInfo)
    question_id: str = ""
    question_title: str = ""
    question_url: str = ""
    html_content: str = ""
    excerpt: str = ""
    created_time: int | None = None
    updated_time: int | None = None
    vote_count: int | None = None
    comment_count: int | None = None
    image_urls: list[str] = field(default_factory=list)
    content_incomplete: bool = False

    @property
    def content_hash(self) -> str:
        basis = self.html_content or self.excerpt or self.title
        return sha256_text(basis)

    @property
    def dedupe_key(self) -> tuple[str, str]:
        if self.zhihu_content_id:
            return (self.content_type, self.zhihu_content_id)
        if self.url:
            return ("url", self.url)
        return ("hash", self.content_hash)

    def as_dict(self) -> dict:
        return {
            "zhihu_content_id": self.zhihu_content_id,
            "content_type": self.content_type,
            "title": self.question_title or self.title,
            "question_id": self.question_id,
            "question_title": self.question_title,
            "question_url": self.question_url,
            "author": self.author.as_dict(),
            "url": self.url,
            "created_time": self.created_time,
            "created_at": unix_to_iso(self.created_time),
            "updated_time": self.updated_time,
            "updated_at": unix_to_iso(self.updated_time),
            "vote_count": self.vote_count,
            "comment_count": self.comment_count,
            "html_content": self.html_content,
            "excerpt": self.excerpt,
            "image_urls": list(self.image_urls),
            "content_hash": self.content_hash,
            "content_incomplete": self.content_incomplete,
        }


def dedupe_records(records: list[ContentRecord]) -> list[ContentRecord]:
    """按内容 ID、URL、正文哈希依次去重，保留先出现的较完整版本。"""
    order: list[tuple[str, str]] = []
    best: dict[tuple[str, str], ContentRecord] = {}
    for record in records:
        key = record.dedupe_key
        current = best.get(key)
        if current is None:
            best[key] = record
            order.append(key)
            continue
        best[key] = _prefer_record(current, record)
    return [best[key] for key in order]


def _prefer_record(current: ContentRecord, incoming: ContentRecord) -> ContentRecord:
    current_len = len(current.html_content or "")
    incoming_len = len(incoming.html_content or "")
    if incoming_len > current_len:
        _fill_missing(incoming, current)
        return incoming
    _fill_missing(current, incoming)
    return current


def _merge_author(target: ContentRecord, source: ContentRecord) -> None:
    destination = target.author
    incoming = source.author
    if not destination.name and incoming.name:
        destination.name = incoming.name
    for attr in ("zhihu_user_id", "headline", "url_token", "url", "avatar_url"):
        if not getattr(destination, attr) and getattr(incoming, attr):
            setattr(destination, attr, getattr(incoming, attr))


def _fill_missing(target: ContentRecord, source: ContentRecord) -> None:
    if not target.html_content and source.html_content:
        target.html_content = source.html_content
    if not target.excerpt and source.excerpt:
        target.excerpt = source.excerpt
    if not target.title and source.title:
        target.title = source.title
    if not target.question_title and source.question_title:
        target.question_title = source.question_title
    if not target.question_id and source.question_id:
        target.question_id = source.question_id
    if not target.question_url and source.question_url:
        target.question_url = source.question_url
    if not target.url and source.url:
        target.url = source.url
    if target.vote_count is None and source.vote_count is not None:
        target.vote_count = source.vote_count
    if target.comment_count is None and source.comment_count is not None:
        target.comment_count = source.comment_count
    if target.created_time is None and source.created_time is not None:
        target.created_time = source.created_time
    if target.updated_time is None and source.updated_time is not None:
        target.updated_time = source.updated_time
    _merge_author(target, source)
    if source.image_urls and not target.image_urls:
        target.image_urls = list(source.image_urls)
    if source.content_incomplete is False and target.html_content:
        target.content_incomplete = False
