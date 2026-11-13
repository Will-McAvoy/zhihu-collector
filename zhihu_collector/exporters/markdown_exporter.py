"""Markdown 是统一正文格式。图片路径按输出文件的位置重写。"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from zhihu_collector.utils.artifacts import image_is_ready
from zhihu_collector.utils.logger import get_logger
from zhihu_collector.utils.time_utils import now_iso, unix_to_iso

logger = get_logger("markdown_exporter")

# 程序自己写入 frontmatter 的字段。只有完全由这些字段组成的开头块才算元数据。
FRONTMATTER_KEYS = frozenset({
    "type", "author", "question", "url", "created_at", "updated_at",
    "vote_count", "comment_count", "title", "exported_at", "count",
})
_FRONTMATTER_LINE = re.compile(r"^([a-z_]+):(?:\s.*)?$")
_MD_IMAGE = re.compile(r'(!\[[^\]]*\]\()(\s*<?)([^)\s>]+)(>?(?:\s+"[^"]*")?\s*\))')
_HTML_IMAGE_SRC = re.compile(r'(<img\b[^>]*?\bsrc=)(["\'])(.*?)(\2)', re.IGNORECASE)


def render_markdown(
    *,
    content_type: str,
    title: str,
    author: str,
    question: str,
    url: str,
    created_time: str | None,
    updated_time: str | None,
    vote_count: int | None,
    comment_count: int | None,
    body: str,
    include_meta: bool,
) -> str:
    body = (body or "").strip() or "（正文为空）"
    if not include_meta:
        return body + "\n"
    lines = [
        "---",
        f"type: {content_type}",
        f"author: {_yaml(author)}",
        f"question: {_yaml(question or title)}",
        f"url: {url}",
        f"created_at: {created_time or ''}",
        f"updated_at: {updated_time or ''}",
        f"vote_count: {vote_count if vote_count is not None else ''}",
        f"comment_count: {comment_count if comment_count is not None else ''}",
        "---",
        "",
        f"# {title or '未命名'}",
        "",
    ]
    if author:
        lines.extend([f"作者：{author}", ""])
    lines.extend([body, "", "---", "", "原文链接：", url, ""])
    return "\n".join(lines)


def render_merged_markdown(*, title: str, author: str, pieces: list[dict], include_meta: bool) -> str:
    """include_meta=False 与单篇一致：不写 frontmatter、作者资料和导出时间，保留标题、目录与正文。"""
    lines: list[str] = []
    if include_meta:
        exported_at = now_iso()
        lines.extend([
            "---",
            f"title: {_yaml(title)}",
            f"author: {_yaml(author)}",
            f"exported_at: {exported_at}",
            f"count: {len(pieces)}",
            "---",
            "",
        ])
    lines.extend([f"# {title}", ""])
    if include_meta:
        lines.extend([
            f"答主：{author or '多位作者'}",
            "",
            f"导出时间：{exported_at}",
            "",
        ])
    lines.extend([
        f"内容总数：{len(pieces)}",
        "",
        "## 目录",
        "",
    ])
    for index, piece in enumerate(pieces, start=1):
        anchor = piece.get("anchor") or f"item-{index}"
        lines.append(f"{index}. [{piece.get('title') or '未命名'}](#{anchor})")
    lines.append("")
    for index, piece in enumerate(pieces, start=1):
        anchor = piece.get("anchor") or f"item-{index}"
        lines.extend(["", "---", "", f"# {piece.get('title') or '未命名'} {{#{anchor}}}", ""])
        if include_meta:
            if piece.get("author"):
                lines.append(f"作者：{piece['author']}")
            if piece.get("created_at"):
                lines.append(f"发布时间：{piece['created_at']}")
            if piece.get("url"):
                lines.append(f"原文链接：{piece['url']}")
            lines.append("")
        lines.append((piece.get("body") or "").strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def split_frontmatter(markdown_text: str, *, present: bool | None = None) -> tuple[dict[str, str], str]:
    """分离程序生成的 frontmatter。

    present=True：调用方明确知道开头是程序写的元数据（仍要求块完整闭合）；
    present=False：没有元数据，原文原样保留——正文本身可以用分隔线开头；
    present=None：只在开头块完整闭合、且每行都是已知元数据字段时才剥离。
    """
    text = markdown_text.replace("\r\n", "\n")
    if present is False:
        return {}, text
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None or closing == 1:
        return {}, text
    inner = lines[1:closing]
    if present is None:
        for line in inner:
            match = _FRONTMATTER_LINE.match(line.strip())
            if match is None or match.group(1) not in FRONTMATTER_KEYS:
                return {}, text
    meta: dict[str, str] = {}
    for line in inner:
        if ":" in line:
            key, value = line.split(":", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                meta[key.strip()] = value
    return meta, "\n".join(lines[closing + 1:])


def localize_images(markdown: str, images: list[dict], markdown_file: Path) -> str:
    """只改写图片节点的地址，按完整 URL（含查询参数）精确匹配。

    本地图片与输出文件不在同一磁盘时无法写相对路径，先复制到输出目录的 images 下；
    复制失败时保留远程地址并记录原因，不中断整个导出。
    """
    mapping: dict[str, str] = {}
    for image in images:
        remote = image.get("original_url") or ""
        local = image.get("local_path") or ""
        if not remote or not local or remote in mapping or not image_is_ready(image):
            continue
        relative = _relative_asset_path(Path(local), markdown_file)
        if relative:
            mapping[remote] = relative
    if not mapping:
        return markdown

    def replace_markdown(match: re.Match) -> str:
        target = mapping.get(match.group(3))
        if target is None:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{target}{match.group(4)}"

    def replace_html(match: re.Match) -> str:
        target = mapping.get(match.group(3))
        if target is None:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{target}{match.group(4)}"

    result = _MD_IMAGE.sub(replace_markdown, markdown)
    return _HTML_IMAGE_SRC.sub(replace_html, result)


def _relative_asset_path(local: Path, markdown_file: Path) -> str:
    try:
        return Path(os.path.relpath(local, markdown_file.parent)).as_posix()
    except ValueError:
        # Windows 上跨盘没有相对路径：把图片放进目标归档自己的资源目录。
        pass
    try:
        copied = _copy_into_assets(local, markdown_file.parent / "images")
    except OSError as exc:
        logger.warning("图片不在输出磁盘且复制失败，保留远程地址：%s（%s）", local, exc)
        return ""
    if copied is None:
        logger.warning("图片不在输出磁盘且本地文件不可用，保留远程地址：%s", local)
        return ""
    return Path(os.path.relpath(copied, markdown_file.parent)).as_posix()


def _copy_into_assets(source: Path, assets_dir: Path) -> Path | None:
    if not source.is_file() or source.stat().st_size <= 0:
        return None
    from zhihu_collector.utils.hashing import sha256_file

    assets_dir.mkdir(parents=True, exist_ok=True)
    candidate = assets_dir / source.name
    if candidate.is_file():
        if candidate.stat().st_size == source.stat().st_size and sha256_file(candidate) == sha256_file(source):
            return candidate
        candidate = assets_dir / f"{source.stem}_{sha256_file(source)[:8]}{source.suffix}"
        if candidate.is_file() and sha256_file(candidate) == sha256_file(source):
            return candidate
    temporary = candidate.with_name(candidate.name + ".tmp")
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(candidate)
    finally:
        if temporary.exists():
            temporary.unlink()
    return candidate


def record_times(created_time: int | None, updated_time: int | None) -> tuple[str | None, str | None]:
    return unix_to_iso(created_time), unix_to_iso(updated_time)


def _yaml(value: str) -> str:
    text = (value or "").replace("\n", " ").strip()
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
