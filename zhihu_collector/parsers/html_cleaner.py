"""把知乎正文整理成干净 HTML，再转成 Markdown。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import MarkdownConverter

from zhihu_collector.parsers.images import extract_image_urls, normalize_image_url

_NOISE_TAGS = {"script", "style", "noscript", "iframe", "button", "form", "input", "svg", "canvas", "textarea"}
_NOISE_CLASS = re.compile(
    r"comment|vote|toolbar|action|share|advert|recommend|modal|sticky|popover|footer|header|nav",
    re.IGNORECASE,
)
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass
class CleanedContent:
    html: str
    markdown: str
    image_urls: list[str]
    video_count: int = 0
    video_fallback_count: int = 0


def clean_content(html: str, *, source_url: str = "") -> CleanedContent:
    cleaned, videos = _clean_html_and_videos(html, source_url=source_url)
    markdown = html_to_markdown(cleaned)
    return CleanedContent(
        html=cleaned,
        markdown=markdown,
        image_urls=extract_image_urls(cleaned),
        video_count=len(videos),
        video_fallback_count=sum(1 for item in videos if item.missing_direct_link),
    )


def clean_html(html: str, *, source_url: str = "") -> str:
    cleaned, _videos = _clean_html_and_videos(html, source_url=source_url)
    return cleaned


def _clean_html_and_videos(html: str, *, source_url: str = ""):
    from zhihu_collector.parsers.video_refs import replace_video_references

    if not html or not str(html).strip():
        return "", []
    soup = BeautifulSoup(str(html), "lxml")
    videos = replace_video_references(soup, source_url)
    for tag in list(soup.find_all(_NOISE_TAGS)):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.attrs is None:
            continue
        class_text = " ".join(tag.get("class") or [])
        identity = f"{class_text} {tag.get('id') or ''}"
        if _NOISE_CLASS.search(identity) and tag.name in {"div", "section", "aside", "span", "ul"}:
            if tag.find(["p", "img", "pre", "blockquote", "table"]) is None:
                tag.decompose()
                continue
        _simplify_tag(soup, tag)
    root = soup.body or soup
    rendered = root.decode_contents().strip()
    return rendered, videos


def plain_preview(html: str, *, source_url: str = "") -> str:
    """给界面预览用的纯文本。会去掉脚本，调用方不应把结果再当 HTML 执行。"""
    if not html:
        return ""
    from zhihu_collector.parsers.video_refs import replace_video_references

    soup = BeautifulSoup(str(html), "lxml")
    replace_video_references(soup, source_url)
    for tag in soup.find_all(["script", "style", "iframe", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


class _ArchiveConverter(MarkdownConverter):
    """表格单元格中的字面竖线按 GFM 写成 \\|，否则会被当成列分隔符。"""

    def convert_td(self, el, text, parent_tags):
        return super().convert_td(el, _UNESCAPED_PIPE.sub(r"\\|", text), parent_tags)

    def convert_th(self, el, text, parent_tags):
        return super().convert_th(el, _UNESCAPED_PIPE.sub(r"\\|", text), parent_tags)


def html_to_markdown(html: str) -> str:
    if not html:
        return ""
    markdown = _ArchiveConverter(
        heading_style="ATX",
        bullets="-",
        code_language_callback=_code_language,
    ).convert(html)
    return _BLANK_LINES.sub("\n\n", markdown).strip()


def unwrap_zhihu_link(href: str) -> str:
    if not href:
        return ""
    parsed = urlparse(href)
    host = (parsed.netloc or "").lower()
    if "link.zhihu.com" not in host:
        return href
    target = parse_qs(parsed.query).get("target", [""])[0]
    return unquote(target) or href


def _simplify_tag(soup: BeautifulSoup, tag: Tag) -> None:
    if tag.name == "a":
        href = unwrap_zhihu_link(str(tag.get("href") or ""))
        tag.attrs = {"href": href} if href else {}
        return
    if tag.name == "img":
        formula = tag.get("data-formula") or tag.get("data-tex")
        if formula:
            tag.replace_with(NavigableString(f"${formula}$"))
            return
        src = ""
        for attr in ("data-original", "data-actualsrc", "data-src", "src"):
            value = tag.get(attr)
            if value and not str(value).startswith("data:"):
                src = normalize_image_url(str(value))
                if src:
                    break
        alt = str(tag.get("alt") or "")
        if not src:
            tag.decompose()
            return
        tag.attrs = {"src": src, "alt": alt}
        return
    if tag.name == "span":
        formula = tag.get("data-tex") or tag.get("data-formula")
        if formula:
            tag.replace_with(NavigableString(f"${formula}$"))
            return
    if tag.name == "code":
        classes = tag.get("class") or []
        language = ""
        for name in classes:
            if str(name).startswith("language-"):
                language = str(name)
                break
        tag.attrs = {"class": [language]} if language else {}
        return
    tag.attrs = {}


def _code_language(element) -> str | None:
    classes = element.get("class") or []
    for name in classes:
        text = str(name)
        if text.startswith("language-"):
            return text.split("-", 1)[1]
    return None
