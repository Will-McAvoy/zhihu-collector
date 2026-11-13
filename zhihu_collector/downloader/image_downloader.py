"""按正文顺序下载图片。失败时保留远程地址，不中断整篇内容。"""

from __future__ import annotations

import mimetypes
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from zhihu_collector.errors import LoginExpiredError
from zhihu_collector.utils.hashing import sha256_file, sha256_text
from zhihu_collector.utils.logger import get_logger
from zhihu_collector.utils.retry import RetryGaveUp, call_with_retry

logger = get_logger("images")

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


@dataclass
class ImageAsset:
    original_url: str
    local_path: str = ""
    download_status: str = "pending"
    hash: str = ""
    sort_index: int = 0
    error: str = ""

    def as_row(self) -> dict:
        return {
            "original_url": self.original_url,
            "local_path": self.local_path,
            "download_status": self.download_status,
            "hash": self.hash,
            "sort_index": self.sort_index,
            "error": self.error,
        }


class ImageDownloader:
    def __init__(
        self,
        *,
        concurrency: int = 3,
        attempts: int = 3,
        base_delay_ms: int = 1500,
        client: httpx.Client | None = None,
    ):
        self.concurrency = max(1, concurrency)
        self.attempts = max(1, attempts)
        self.base_delay_ms = base_delay_ms
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.zhihu.com/",
            },
        )
        self.bytes_downloaded = 0
        self._bytes_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def download_all(
        self,
        urls: list[str],
        directory: Path,
        content_id: str,
        *,
        indexes: list[int] | None = None,
        known_hashes: dict[str, str] | None = None,
    ) -> list[ImageAsset]:
        directory.mkdir(parents=True, exist_ok=True)
        unique_urls = list(dict.fromkeys(urls))
        if not unique_urls:
            return []
        known_hashes = known_hashes or {}

        def fetch_one(item: tuple[int, str]) -> ImageAsset:
            index, url = item
            return self._download_one(
                url,
                directory,
                content_id,
                index,
                known_hash=known_hashes.get(url, ""),
            )

        if indexes and len(indexes) == len(unique_urls):
            pairs = list(zip(indexes, unique_urls, strict=True))
        else:
            pairs = list(enumerate(unique_urls, start=1))
        if self.concurrency == 1 or len(pairs) == 1:
            assets = [fetch_one(pair) for pair in pairs]
        else:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                assets = list(pool.map(fetch_one, pairs))
        assets.sort(key=lambda asset: asset.sort_index)
        return assets

    def _download_one(
        self,
        url: str,
        directory: Path,
        content_id: str,
        index: int,
        *,
        known_hash: str = "",
    ) -> ImageAsset:
        asset = ImageAsset(original_url=url, sort_index=index)
        extension = _extension_from_url(url)
        filename = f"{_safe_id(content_id)}_{sha256_text(url)[:12]}{extension}"
        destination = directory / filename
        marker = destination.with_name(destination.name + ".url")
        if _file_belongs_to_url(destination, marker, url, known_hash):
            asset.local_path = str(destination)
            asset.download_status = "downloaded"
            asset.hash = sha256_file(destination)
            return asset
        try:
            response = call_with_retry(
                lambda: self._get(url),
                attempts=self.attempts,
                base_delay_ms=self.base_delay_ms,
                retry_on=(httpx.HTTPError, OSError, TimeoutError),
            )
        except LoginExpiredError as exc:
            asset.download_status = "failed"
            asset.error = str(exc)
            return asset
        except (RetryGaveUp, httpx.HTTPError, OSError) as exc:
            logger.warning("图片下载失败，保留远程地址 %s：%s", url, exc)
            asset.download_status = "failed"
            asset.error = str(exc)
            return asset
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        rejected = image_payload_problem(content_type, response.content)
        if rejected:
            logger.warning("图片响应不是图片，保留远程地址 %s：%s", url, rejected)
            asset.download_status = "failed"
            asset.error = rejected
            return asset
        if extension == ".img" and content_type in _EXT_BY_TYPE:
            destination = directory / f"{_safe_id(content_id)}_{index:02d}{_EXT_BY_TYPE[content_type]}"
        try:
            destination.write_bytes(response.content)
            marker.write_text(url, encoding="utf-8")
            asset.local_path = str(destination)
            asset.download_status = "downloaded"
            asset.hash = sha256_file(destination)
            with self._bytes_lock:
                self.bytes_downloaded += len(response.content)
        except OSError as exc:
            logger.warning("图片写入失败 %s：%s", destination, exc)
            asset.download_status = "failed"
            asset.error = f"图片写入失败：{exc}"
        return asset

    def _get(self, url: str) -> httpx.Response:
        response = self.client.get(url)
        if response.status_code in {401, 403} and "登录" in response.text[:500]:
            raise LoginExpiredError("下载图片时知乎要求登录。请重新登录后再试。")
        response.raise_for_status()
        return response


_IMAGE_SIGNATURES = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
)
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml", "application/javascript")


def _has_image_signature(content: bytes) -> bool:
    if any(content.startswith(signature) for signature in _IMAGE_SIGNATURES):
        return True
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return True
    # AVIF / HEIC: ISO-BMFF ftyp box
    return content[4:8] == b"ftyp" and content[8:12] in {b"avif", b"avis", b"heic", b"heix", b"mif1", b"msf1"}


def _looks_like_markup_or_json(content: bytes) -> bool:
    head = content[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<!doctype", b"<html", b"<head", b"<body", b"<?xml", b"<error")):
        return b"<svg" not in content[:2048].lower()
    return head.startswith((b"{", b"["))


def image_payload_problem(content_type: str, content: bytes) -> str:
    """HTTP 200 不代表拿到了图片：拒绝空响应和 HTML/JSON 错误页。"""
    if not content:
        return "图片响应为空"
    if content_type == "image/svg+xml":
        return ""
    if content_type.startswith(_TEXT_TYPES) or _looks_like_markup_or_json(content):
        if _has_image_signature(content):
            return ""
        return f"服务器返回的不是图片（{content_type or '未知类型'}），可能是错误页或需要登录"
    if content_type.startswith("image/"):
        return ""
    if _has_image_signature(content):
        return ""
    return f"无法确认响应是图片（{content_type or '未知类型'}）"


def _file_belongs_to_url(path: Path, marker: Path, url: str, known_hash: str) -> bool:
    """只有标记确实属于这个 URL 的文件才能直接复用，不能只看文件名存在。"""
    if not path.is_file() or path.stat().st_size <= 0 or not marker.is_file():
        return False
    try:
        if marker.read_text(encoding="utf-8").strip() != url:
            return False
    except OSError:
        return False
    if known_hash and sha256_file(path) != known_hash:
        return False
    return True


def _extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(suffix) if False else ""
    return guessed or ".jpg"


def _safe_id(content_id: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in content_id)
    return cleaned[:80] or "content"
