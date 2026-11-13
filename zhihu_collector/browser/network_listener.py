"""监听当前页面自己发出的 JSON 响应。

不伪造请求头，不生成知乎签名，只读取这次浏览器会话里已经返回的数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
import re
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from zhihu_collector.errors import TaskStopped
from zhihu_collector.browser.guarded import BrowserOperationTimeout
from zhihu_collector.utils.list_target import describe_paging
from zhihu_collector.utils.logger import get_logger
from zhihu_collector.utils.urls import is_zhihu_host

logger = get_logger("network")

_INTERESTING_PARTS = (
    "/api/v4/members/",
    "/api/v4/questions/",
    "/api/v4/answers/",
    "/api/v4/pins/",
    "/api/v4/v2/pins/",
    "/api/v4/comment_v5/",
    "/api/v4/collections/",
    "/api/v4/favlists/",
    "/api/v3/collections/",
)


@dataclass
class CapturedResponse:
    url: str
    status: int
    data: Any
    content_type: str = ""
    paging: dict | None = None


@dataclass
class NetworkListener:
    payloads: list[CapturedResponse] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    last_paging: dict | None = None
    total_captured: int = 0
    _lock: Lock = field(default_factory=Lock)

    _pending: deque = field(default_factory=deque)
    _inflight: dict = field(default_factory=dict)
    _guard: Any = None
    _draining: bool = False

    def _drain(self):
        if self._draining:
            return
        self._draining = True
        try:
            with self._lock:
                ready = list(self._pending)
                self._pending.clear()
            for response in ready:
                self._capture(response)
        finally:
            self._draining = False

    @property
    def has_listing_payload(self) -> bool:
        self._drain()
        with self._lock:
            return any(_is_listing_url(item.url) for item in self.payloads)

    def attach(self, page) -> None:
        self._guard = getattr(page, "run_guarded", None)
        page.on("response", self._on_response)
        page.on("requestfinished", self._on_finished)
        page.on("requestfailed", self._on_failed)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._inflight.clear()
            self.payloads.clear()
            self.errors.clear()
            self.last_paging = None
            self.total_captured = 0

    def consume(self) -> list[CapturedResponse]:
        """取出尚未处理的响应。分页信息仍保留在 last_paging。"""
        self._drain()
        with self._lock:
            items = list(self.payloads)
            self.payloads.clear()
            return items

    def consume_errors(self) -> list[dict]:
        """取出尚未处理的接口错误，避免旧错误在后续每一轮被重复判断。"""
        self._drain()
        with self._lock:
            items = list(self.errors)
            self.errors.clear()
            return items

    def snapshot(self) -> list[CapturedResponse]:
        self._drain()
        with self._lock:
            return list(self.payloads)

    def snapshot_data(self) -> list:
        return [item.data for item in self.snapshot() if item.status < 400]

    def summary(self) -> list[dict]:
        rows = []
        for item in self.snapshot():
            data = item.data
            count = None
            keys: list[str] = []
            paging = None
            if isinstance(data, dict):
                keys = list(data.keys())[:12]
                payload = data.get("data")
                if isinstance(payload, list):
                    count = len(payload)
                paging_raw = data.get("paging")
                if isinstance(paging_raw, dict):
                    paging = {
                        "is_end": paging_raw.get("is_end"),
                        "totals": paging_raw.get("totals"),
                        "next": bool(paging_raw.get("next")),
                    }
            rows.append(
                {
                    "url": item.url.split("?", 1)[0],
                    "status": item.status,
                    "keys": keys,
                    "data_count": count,
                    "paging": paging,
                }
            )
        return rows

    @staticmethod
    def _request_key(request):
        return id(getattr(request, "_impl_obj", request))

    def _on_response(self, response) -> None:
        # Header/finished callbacks perform no synchronous browser RPC.
        if not _is_interesting(response.url):
            return
        with self._lock:
            if len(self._inflight) >= 256:
                self._inflight.pop(next(iter(self._inflight)))
            self._inflight[self._request_key(response.request)] = response
            if response.status >= 400:
                self.errors.append({"url": response.url.split("?", 1)[0], "status": response.status, "message": ""})

    def _on_finished(self, request):
        with self._lock:
            response = self._inflight.pop(self._request_key(request), None)
            if response is not None:
                if len(self._pending) >= 256:
                    self._pending.popleft()
                self._pending.append(response)

    def _on_failed(self, request):
        with self._lock:
            self._inflight.pop(self._request_key(request), None)

    def _capture(self, response) -> None:
        from zhihu_collector.browser.sync_guard import install
        install(response)
        url = response.url
        if not _is_interesting(url):
            return
        status = response.status
        content_type = ""
        try:
            content_type = response.headers.get("content-type", "")
        except Exception:
            content_type = ""
        try:
            data = self._guard("读取已完成的接口正文", response.json, 12) if callable(self._guard) else response.json()
        except (TaskStopped, BrowserOperationTimeout):
            raise
        except Exception as exc:
            logger.debug("无法解析响应 %s：%s", url.split("?", 1)[0], exc)
            if status >= 400:
                with self._lock:
                    self.errors.append({"url": url.split("?", 1)[0], "status": status, "message": ""})
            return
        paging = describe_paging(data) if status < 400 else None
        captured = CapturedResponse(
            url=url,
            status=status,
            data=data,
            content_type=content_type,
            paging=paging,
        )
        with self._lock:
            self.payloads.append(captured)
            self.total_captured += 1
            # last_paging 只给“是否已经有列表响应”这种等待判断使用。
            # 停止翻页必须走目标列表自己的分页，不能读这个无来源字段。
            if isinstance(paging, dict) and _is_listing_url(url):
                self.last_paging = {
                    "is_end": paging.get("is_end") is True,
                    "totals": paging.get("totals"),
                    "next": paging.get("next") or "",
                    "has_next": paging.get("has_next"),
                }
            if status >= 400 or _payload_has_error(data):
                self.errors.append(
                    {
                        "url": url.split("?", 1)[0],
                        "status": status,
                        "message": _error_message(data),
                        "code": _error_code(data),
                    }
                )
        logger.info(
            "捕获页面响应 %s status=%s",
            url.split("?", 1)[0],
            status,
        )


def _is_listing_url(url: str) -> bool:
    path = urlparse(url).path
    if path.startswith("/api/v4/v2/pins/") and path.rstrip("/").endswith("/moments"):
        return True
    if "/answers/" in path or "/pins/" in path:
        return False
    return any(part in path for part in ("/answers", "/pins", "/feeds", "/collections/", "/favlists/"))


def _is_interesting(url: str) -> bool:
    parsed = urlparse(url)
    if not is_zhihu_host(parsed.hostname):
        return False
    path = parsed.path or ""
    return bool(re.fullmatch(
        r"/api/v[34]/(?:(?:members/[^/]+/(?:answers|pins|feeds))|(?:questions/\d+/answers)|"
        r"(?:(?:answers|pins)/\d+)|(?:(?:collections|favlists)/\d+(?:/items)?)|(?:v2/pins/[^/]+/moments))/?", path
    ))


def _payload_has_error(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("error"), dict):
        return True
    code = data.get("code")
    return "data" not in data and (
        str(code).isdigit() and int(code) >= 400
        or any(hint in _error_message(data) for hint in ("请先登录", "请您登录", "未登录", "登录已失效"))
    )


def _error_code(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    return str((error.get("code") if isinstance(error, dict) else data.get("code")) or "")


def _error_message(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")
    return str(data.get("message") or "")
