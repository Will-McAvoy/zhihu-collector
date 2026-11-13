"""把回答正文里的视频块换成可核对的观看入口。

只保留标题和 http/https 链接，不下载视频，也不留下可执行的播放器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from zhihu_collector.utils.list_target import _CREDENTIAL_KEYS

_VIDEO_CLASS = re.compile(r"(^|[\s_-])(video-box|video-card|richtext-video|zvideo|videoitem)([\s_-]|$)", re.IGNORECASE)
_MEDIA_SUFFIXES = (".mp4", ".m3u8", ".webm", ".m4s", ".flv", ".ts")
_PLAYER_HOSTS = (
    "bilibili.com",
    "youtube.com",
    "youtu.be",
    "youku.com",
    "v.qq.com",
    "iqiyi.com",
    "player.zhihu.com",
)
_MISSING_TITLE = "视频（原文未提供标题）"


@dataclass
class VideoRef:
    title: str
    watch_url: str
    cover_url: str
    source: str
    link_kind: str
    missing_direct_link: bool
    position: int


def html_has_video_reference(raw_html: str) -> bool:
    if not raw_html or "<" not in raw_html:
        return False
    soup = BeautifulSoup(str(raw_html), "lxml")
    return any(_is_video_node(node) for node in soup.find_all(True))


def replace_video_references(soup: BeautifulSoup, source_url: str = "") -> list[VideoRef]:
    """就地替换视频节点。同一位置的嵌套卡片只生成一个占位。"""
    refs: list[VideoRef] = []
    answer_url = _safe_http_url(source_url)
    for node in list(soup.find_all(True)):
        if not isinstance(node, Tag) or not _is_attached(node, soup):
            continue
        if not _is_video_node(node):
            continue
        ref = _extract(node, answer_url, len(refs) + 1)
        node.replace_with(_placeholder(soup, ref))
        refs.append(ref)
    return refs


def _is_video_node(node: Tag) -> bool:
    if _is_our_placeholder(node):
        return False
    name = (node.name or "").lower()
    if name == "video":
        return True
    if name == "source" and node.find_parent("video") is None:
        return bool(_safe_http_url(str(node.get("src") or "")))
    if name == "iframe":
        return _is_video_target(str(node.get("src") or ""))
    classes = " ".join(str(item) for item in (node.get("class") or []))
    identity = f"{classes} {node.get('id') or ''}"
    has_marker = bool(_VIDEO_CLASS.search(identity)) or any(
        node.get(attr) for attr in ("data-lens-id", "data-video-id", "data-zvideo-id")
    )
    if name == "a":
        href = _safe_http_url(str(node.get("href") or ""))
        wraps_image = node.find("img") is not None
        return (has_marker or (wraps_image and _is_video_page(href))) and bool(href or has_marker)
    if has_marker and name in {"div", "section", "figure", "span", "p"}:
        return True
    return False


def _is_attached(node: Tag, soup: BeautifulSoup) -> bool:
    current = node
    while current is not None:
        if current is soup or current is soup.body:
            return True
        current = current.parent
    return False


def _is_our_placeholder(node: Tag) -> bool:
    classes = {str(item) for item in (node.get("class") or [])}
    return "zhihu-media-ref" in classes


def _extract(node: Tag, answer_url: str, position: int) -> VideoRef:
    title = _title(node)
    cover = _cover(node)
    watch = _watch_url(node)
    source = _source_name(node)
    if watch and _is_video_page(watch):
        return VideoRef(title, watch, cover, source, "video_page", False, position)
    if answer_url:
        return VideoRef(title, answer_url, cover, source, "answer_fallback", True, position)
    return VideoRef(title, "", cover, source, "answer_fallback", True, position)


def _placeholder(soup: BeautifulSoup, ref: VideoRef) -> Tag:
    block = soup.new_tag("blockquote")
    block["class"] = ["zhihu-media-ref"]
    title = soup.new_tag("p")
    title.string = f"[视频] {ref.title}"
    block.append(title)
    if ref.cover_url:
        figure = soup.new_tag("p")
        image = soup.new_tag("img")
        image["src"] = ref.cover_url
        image["alt"] = ref.title
        figure.append(image)
        block.append(figure)
    note = soup.new_tag("p")
    if ref.missing_direct_link:
        if ref.watch_url:
            note.string = "未取得独立视频链接。视频未下载，请前往原回答查看。"
        else:
            note.string = "未取得独立视频链接。视频未下载。"
    else:
        note.string = "视频未下载，需联网观看。"
    block.append(note)
    if ref.watch_url:
        line = soup.new_tag("p")
        line.append("原回答：" if ref.missing_direct_link else "观看视频：")
        link = soup.new_tag("a")
        link["href"] = ref.watch_url
        link.string = ref.watch_url
        line.append(link)
        block.append(line)
    return block


def _title(node: Tag) -> str:
    for attr in ("data-name", "data-title", "title", "aria-label"):
        text = _clean_title(node.get(attr))
        if text:
            return text
    image = node.find("img")
    if isinstance(image, Tag):
        text = _clean_title(image.get("alt"))
        if text and text not in {"视频", "video"}:
            return text
    text = _clean_title(node.get_text(" ", strip=True))
    if text and text not in {"视频", "video", "观看视频"} and len(text) <= 80:
        return text
    return _MISSING_TITLE


def _clean_title(value) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value.replace("\r", " ").replace("\n", " ") if ch.isprintable())
    text = " ".join(text.split()).replace("[", "［").replace("]", "］")
    return text[:120]


def _cover(node: Tag) -> str:
    for attr in ("poster", "data-poster", "data-cover", "data-thumbnail"):
        url = _safe_http_url(str(node.get(attr) or ""))
        if url and not _is_media_file(url):
            return url
    image = node.find("img")
    if isinstance(image, Tag):
        for attr in ("data-original", "data-actualsrc", "data-src", "src"):
            url = _safe_http_url(str(image.get(attr) or ""))
            if url and not _is_media_file(url):
                return url
    return ""


def _watch_url(node: Tag) -> str:
    candidates: list[str] = []
    if isinstance(node, Tag) and node.name == "a":
        candidates.append(str(node.get("href") or ""))
    for link in node.find_all("a"):
        candidates.append(str(link.get("href") or ""))
    if node.name == "iframe":
        candidates.append(str(node.get("src") or ""))
    for attr in ("data-url", "data-href", "data-landing"):
        if node.get(attr):
            candidates.append(str(node.get(attr)))
    for candidate in candidates:
        url = _safe_http_url(candidate)
        if url and _is_video_page(url):
            return url
    return ""


def _source_name(node: Tag) -> str:
    name = (node.name or "").lower()
    if name == "iframe":
        return "iframe"
    if name in {"video", "source"}:
        return "video"
    if name == "a":
        return "link"
    return "card"


def _is_video_target(url: str) -> bool:
    safe = _safe_http_url(url)
    if not safe:
        return False
    return _is_video_page(safe) or _is_media_file(safe)


def _is_video_page(url: str) -> bool:
    if not url or _is_media_file(url):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if "zhihu.com" in host and ("/zvideo/" in path or path.startswith("/zvideo") or "/video/" in path):
        return True
    return any(host == name or host.endswith("." + name) for name in _PLAYER_HOSTS)


def _is_media_file(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(_MEDIA_SUFFIXES)


def _safe_http_url(url: str) -> str:
    if not url:
        return ""
    candidate = _unwrap_zhihu_link(str(url).strip())
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() not in _CREDENTIAL_KEYS and not key.lower().startswith("x-amz-")
    ]
    return urlunparse((scheme, parsed.netloc, parsed.path, "", urlencode(query), ""))


def _unwrap_zhihu_link(href: str) -> str:
    if not href:
        return ""
    parsed = urlparse(href)
    host = (parsed.netloc or "").lower()
    if "link.zhihu.com" not in host:
        return href
    target = parse_qsl(parsed.query)
    for key, value in target:
        if key == "target" and value:
            return unquote(value)
    return href
