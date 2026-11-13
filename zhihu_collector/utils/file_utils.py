"""Windows 安全文件名。"""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(
    name: str,
    *,
    fallback: str = "untitled",
    max_length: int = 80,
    extension: str = "",
) -> str:
    """把任意标题变成 Windows 上可用的文件名（不含目录）。"""
    text = _WHITESPACE.sub(" ", (name or "").strip())
    text = _INVALID_CHARS.sub("_", text)
    text = text.strip(" .")
    if not text:
        text = fallback
    stem = Path(text).stem if extension and text.lower().endswith(extension.lower()) else text
    # 保留名按不区分大小写判断，且忽略末尾的点和空格。
    reserved_probe = stem.split(".", 1)[0].upper()
    if reserved_probe in _RESERVED:
        stem = f"_{stem}"
    ext = extension if not extension or extension.startswith(".") else f".{extension}"
    # 按字符数限制，给扩展名和去重后缀留余量。
    room = max(8, max_length - len(ext))
    if len(stem) > room:
        stem = stem[:room].rstrip(" .")
    if not stem:
        stem = fallback
    return f"{stem}{ext}"


def unique_path(path: Path) -> Path:
    """若路径已存在，追加 _2、_3……避免覆盖。"""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    number = 2
    while True:
        candidate = parent / f"{stem}_{number}{suffix}"
        if not candidate.exists():
            return candidate
        number += 1


def apply_filename_rule(
    rule: str,
    *,
    index: int,
    title: str,
    content_id: str,
    extension: str,
) -> str:
    """按设置中的命名规则生成文件名。规则支持 {index}、{title}、{id}。"""
    safe_title = safe_filename(title, fallback=content_id or "untitled", max_length=60)
    try:
        for _, field, spec, conversion in Formatter().parse(rule):
            if field is None:
                continue
            if field not in {"index", "title", "id"} or conversion or "{" in spec or "}" in spec:
                raise ValueError("不支持的文件名模板字段")
            if any(int(n) > 256 for n in re.findall(r"\d+", spec)):
                raise ValueError("文件名模板的字段宽度过大")
        rendered = rule.format(index=index, title=safe_title, id=content_id or str(index))
    except (KeyError, ValueError, IndexError):
        rendered = f"{index:04d}-{safe_title}"
    return safe_filename(rendered, fallback=content_id or "untitled", extension=extension)
