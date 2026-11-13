"""从正文 HTML 中按出现顺序提取图片 URL。"""

from __future__ import annotations

from bs4 import BeautifulSoup

_ATTRS = ("data-original", "data-actualsrc", "data-src", "src")


def extract_image_urls(html: str) -> list[str]:
    if not html or "<" not in html:
        return []
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    seen: set[str] = set()
    for image in soup.find_all("img"):
        raw = ""
        for attr in _ATTRS:
            value = image.get(attr)
            if value and not str(value).startswith("data:"):
                raw = str(value).strip()
                break
        normalized = normalize_image_url(raw)
        if not normalized or normalized in seen:
            continue
        if _looks_like_decoration(normalized):
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def normalize_image_url(url: str) -> str:
    if not url:
        return ""
    text = url.strip()
    if text.startswith("//"):
        text = f"https:{text}"
    if not text.startswith("http://") and not text.startswith("https://"):
        return ""
    return text


def _looks_like_decoration(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in ("/avatar/", "default_avatar", "1x1", "blank.gif"))
