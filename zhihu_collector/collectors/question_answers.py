"""采集一个问题下可以正常打开的公开回答。"""

from __future__ import annotations

from zhihu_collector.collectors.user_answers import records_from_page_sources
from zhihu_collector.models import ContentRecord


def parse_question_page(payloads: list, html: str, *, question_id: str) -> list[ContentRecord]:
    records = records_from_page_sources(payloads=payloads, html=html, user_token="")
    if not question_id:
        return records
    matched = [
        record
        for record in records
        if record.question_id == question_id or f"/question/{question_id}/" in (record.url or "")
    ]
    return matched or records
