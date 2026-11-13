"""采集答主公开想法。"""

from __future__ import annotations

from zhihu_collector.models import ContentRecord, dedupe_records
from zhihu_collector.parsers.pin_parser import parse_pin_payloads


def parse_pin_page(payloads: list, html: str) -> list[ContentRecord]:
    del html
    return dedupe_records(parse_pin_payloads(payloads))
