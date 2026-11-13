"""导出产物与本地图片的健康检查。缓存命中、失败列表、重试和重新导出共用这里的判断。"""

from __future__ import annotations

import threading
import zipfile
from pathlib import Path

from zhihu_collector.utils.hashing import sha256_file

_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_HASH_CACHE_LOCK = threading.Lock()


def cached_sha256(path: Path) -> str:
    """按 (路径, 大小, 修改时间) 缓存文件摘要；文件一变就重新计算。"""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _HASH_CACHE_LOCK:
        cached = _HASH_CACHE.get(key)
    if cached is not None:
        return cached
    value = sha256_file(path)
    with _HASH_CACHE_LOCK:
        if len(_HASH_CACHE) > 20000:
            _HASH_CACHE.clear()
        _HASH_CACHE[key] = value
    return value


def file_digest(path: Path) -> tuple[int, str]:
    return path.stat().st_size, cached_sha256(path)


def artifact_problem(path: Path, export_format: str) -> str:
    """返回产物问题描述；健康时返回空字符串。只看文件本身，不涉及是否过期。"""
    try:
        if not path.is_file():
            return "文件不存在"
        size = path.stat().st_size
    except OSError as exc:
        return f"无法读取文件：{exc}"
    if size <= 0:
        return "文件为空"
    try:
        if export_format == "markdown":
            path.read_bytes().decode("utf-8")
        elif export_format == "docx":
            if not zipfile.is_zipfile(path):
                return "不是有效的 Word 文件"
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                    return "Word 文件缺少正文结构"
                if archive.testzip() is not None:
                    return "Word 文件内容损坏"
        elif export_format == "pdf":
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    return "不是有效的 PDF 文件"
                handle.seek(max(0, size - 4096))
                if not handle.read().rstrip().endswith(b"%%EOF"):
                    return "PDF 文件缺少结束标记，可能已截断"
    except UnicodeDecodeError:
        return "Markdown 不是有效的 UTF-8 文本"
    except (OSError, zipfile.BadZipFile) as exc:
        return f"文件损坏：{exc}"
    return ""


def image_problem(image: dict, *, verify_hash: bool = True) -> str:
    """已标记下载成功的图片是否仍然可用：文件存在、非空、与登记的摘要一致。"""
    if image.get("download_status") != "downloaded":
        return str(image.get("error") or image.get("download_status") or "未下载")
    local = image.get("local_path") or ""
    if not local:
        return "没有本地文件路径"
    path = Path(local)
    try:
        if not path.is_file():
            return "本地文件缺失"
        if path.stat().st_size <= 0:
            return "本地文件为空"
        expected = image.get("hash") or ""
        if verify_hash and expected and _looks_like_sha256(expected) and cached_sha256(path) != expected:
            return "本地文件与登记的摘要不一致"
    except OSError as exc:
        return f"无法读取本地文件：{exc}"
    return ""


def image_is_ready(image: dict) -> bool:
    return not image_problem(image)


def _looks_like_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower())
