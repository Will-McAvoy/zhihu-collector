"""读取页面里知乎自己嵌入的初始 JSON。"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

_ASSIGN_RE = re.compile(
    r"(?<![\w.])(?:window\.)?__(?:INITIAL_STATE|INITIAL_DATA|NEXT_DATA)__\s*=\s*(?=[{\[])",
    re.DOTALL,
)


def extract_embedded_payloads(html: str) -> list[Any]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    payloads: list[Any] = []
    for node in soup.select("#js-initialData, script#js-initialData, script[type='application/json']"):
        text = node.string or node.get_text() or ""
        parsed = _loads(text)
        if parsed is not None:
            payloads.append(parsed)
    for script in soup.find_all("script"):
        text = script.string or ""
        decoder = json.JSONDecoder()
        for match in _ASSIGN_RE.finditer(text):
            try:
                parsed, _ = decoder.raw_decode(text, match.end())
            except (json.JSONDecodeError, RecursionError):
                continue
            payloads.append(parsed)
    return payloads


def _loads(text: str) -> Any | None:
    stripped = (text or "").strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
