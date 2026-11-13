"""为文章预览生成安全、可读的受限富文本 HTML。

支持本地图片渲染、缺失占位、长文阅读样式及标签清洗（过滤恶意脚本、iframe、事件处理器等）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from bs4 import BeautifulSoup

import markdown
from zhihu_collector.parsers.html_cleaner import clean_html

_DANGEROUS_TAGS = {"script", "iframe", "object", "embed", "form", "base", "meta", "link"}
_DANGEROUS_SCHEMES = ("javascript:", "data:", "vbscript:")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def sanitize_preview_html(raw_html: str) -> str:
    """彻底移除不可信脚本、危险标签与内联事件属性。"""
    if not raw_html or not raw_html.strip():
        return ""

    soup = BeautifulSoup(raw_html, "lxml")

    # 移除危险标签
    for tag in soup.find_all(_DANGEROUS_TAGS):
        tag.decompose()

    # 移除内联事件处理器与危险链接协议
    for element in soup.find_all(True):
        attrs = dict(element.attrs)
        for attr, val in attrs.items():
            if attr.lower().startswith("on"):
                del element.attrs[attr]
            elif attr.lower() in ("href", "src"):
                val_str = str(val).strip().lower()
                if any(val_str.startswith(s) for s in _DANGEROUS_SCHEMES):
                    element.attrs[attr] = "#"

    body = soup.body
    return "".join(str(c) for c in body.children) if body else str(soup)


def render_article_preview(
    row: dict | Any,
    images: list[dict],
    base_font_size: int = 15,
    allowed_roots: list[Path | str] | None = None,
) -> str:
    """基于保存的内容和本地配图生成可阅读的受控 HTML。"""
    raw_html = row["html_content"] if (row and row["html_content"]) else ""
    if not raw_html and row and row["markdown_content"]:
        raw_html = markdown.markdown(row["markdown_content"], extensions=["tables", "fenced_code"])

    cleaned_body = sanitize_preview_html(clean_html(raw_html)) if raw_html else "<p>（暂无正文）</p>"

    # 替换图片为已下载的本地受控路径或占位框
    soup = BeautifulSoup(cleaned_body, "lxml")

    # 构建 url -> local_path 映射
    image_map: dict[str, str] = {}
    for img in images:
        orig = (img.get("original_url") or "").strip()
        local = (img.get("local_path") or "").strip()
        if orig and local:
            image_map[orig] = local

    # 遍历所有 img 标签
    for img_tag in soup.find_all("img"):
        src = (img_tag.get("src") or img_tag.get("data-actualsrc") or img_tag.get("data-original") or "").strip()
        local_path = image_map.get(src)

        if local_path and Path(local_path).is_file() and _is_allowed_local_path(local_path, allowed_roots):
            # 使用本地文件协议，受限显示
            resolved = Path(local_path).resolve().as_posix()
            img_tag["src"] = f"file:///{resolved}"
            img_tag["style"] = "max-width: 100%; height: auto; display: block; margin: 12px auto; border-radius: 4px;"
            # 移除可能带有的外部 data 属性
            for extra in ("data-actualsrc", "data-original", "data-default-watermark-src"):
                if extra in img_tag.attrs:
                    del img_tag.attrs[extra]
        else:
            # 图片未下载或本地文件缺失时展示占位框，正文仍可阅读
            placeholder = soup.new_tag("div")
            placeholder["style"] = (
                "background-color: #f1f3f4; border: 1px dashed #c0c4cc; color: #5f6368; "
                "padding: 10px; margin: 10px 0; text-align: center; font-size: 13px; "
                "border-radius: 4px; user-select: none;"
            )
            placeholder.string = "【图片未下载或本地文件缺失】"
            img_tag.replace_with(placeholder)

    body_html = "".join(str(c) for c in (soup.body.children if soup.body else soup.children))

    # 生成带整洁阅读样式的完整 HTML
    html_template = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: {base_font_size}px;
    line-height: 1.75;
    color: #202124;
    padding: 18px 24px;
    margin: 0;
    word-break: break-word;
}}
h1, h2, h3, h4, h5, h6 {{
    color: #1a0dab;
    line-height: 1.4;
    margin-top: 1.2em;
    margin-bottom: 0.6em;
}}
p {{
    margin-top: 0.8em;
    margin-bottom: 0.8em;
}}
blockquote {{
    margin: 1em 0;
    padding: 10px 16px;
    background-color: #f8f9fa;
    border-left: 4px solid #dadce0;
    color: #3c4043;
}}
pre, code {{
    font-family: Consolas, Monaco, "Courier New", monospace;
    font-size: 0.9em;
    background-color: #f1f3f4;
    border-radius: 3px;
}}
pre {{
    padding: 12px;
    overflow-x: auto;
    border: 1px solid #e8eaed;
}}
code {{
    padding: 2px 4px;
}}
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 1em 0;
}}
th, td {{
    border: 1px solid #dadce0;
    padding: 8px 12px;
    text-align: left;
}}
th {{
    background-color: #f1f3f4;
    font-weight: 600;
}}
a {{
    color: #1a73e8;
    text-decoration: none;
}}
a:hover {{
    text-decoration: underline;
}}
ul, ol {{
    padding-left: 24px;
}}
li {{
    margin-bottom: 4px;
}}
</style>
</head>
<body>
{body_html}
</body>
</html>"""
    return html_template


def registered_image_roots(image_paths: list[str]) -> list[Path]:
    """已登记且路径本身合法的图片所在目录，用于阅读历史输出位置。"""
    roots: list[Path] = []
    for local in image_paths:
        if not _is_registered_image_file(local):
            continue
        parent = Path(os.path.realpath(local)).resolve().parent
        if parent not in roots:
            roots.append(parent)
    return roots


def _is_registered_image_file(local_path: str) -> bool:
    raw_text = (local_path or "").strip()
    if not raw_text:
        return False
    lowered = raw_text.lower()
    if lowered.startswith(("file:", "javascript:", "data:", "vbscript:", "http:", "https:")):
        return False
    raw = Path(raw_text)
    if any(part == ".." for part in raw.parts):
        return False
    if raw.suffix.lower() not in _IMAGE_SUFFIXES:
        return False
    try:
        lexical = raw.resolve()
        real = Path(os.path.realpath(raw_text)).resolve()
    except OSError:
        return False
    return lexical == real and real.is_file()


def _is_allowed_local_path(local_path: str, allowed_roots: list[Path | str] | None) -> bool:
    """校验本地资源真实路径位于登记资源根目录内，阻止路径穿越和链接逃逸。"""
    if allowed_roots is None:
        return True
    try:
        candidate = Path(os.path.realpath(local_path)).resolve()
        if not candidate.is_file():
            return False
        for root in allowed_roots:
            root_path = Path(os.path.realpath(str(root))).resolve()
            if candidate.is_relative_to(root_path):
                return True
    except (OSError, ValueError):
        return False
    return False
