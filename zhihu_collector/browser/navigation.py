"""页面导航的脱敏诊断；不把网络故障解释为登录失效或验证码。"""

from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlsplit
from urllib.request import proxy_bypass

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from zhihu_collector.utils.logger import get_logger

logger = get_logger("connection")
_ERROR_CODE = re.compile(r"\b(ERR_[A-Z0-9_]+)\b")


def safe_destination(url: str) -> dict:
    try:
        parsed = urlsplit(url)
        return {"host": parsed.hostname or "", "path": parsed.path or "/"}
    except (TypeError, ValueError):
        return {"host": "", "path": ""}


def proxy_flags(host: str) -> dict:
    """只记录是否配置，不记录代理地址、账号、口令或认证文件。"""
    system = False
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                system = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
        except OSError:
            pass
    try:
        bypassed = bool(proxy_bypass(host)) if host else None
    except (OSError, ValueError):
        bypassed = None
    return {
        "environment_proxy_configured": any(os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")),
        "windows_proxy_enabled": system,
        "target_bypassed_by_proxy_rules": bypassed,
    }


def _category(code: str, exc: BaseException) -> str:
    if "TargetClosed" in type(exc).__name__ or "Target page, context or browser has been closed" in str(exc):
        return "browser_closed"
    if code in {"ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED", "ERR_DNS_TIMED_OUT"}:
        return "dns"
    if code in {"ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED", "ERR_NO_SUPPORTED_PROXIES", "ERR_PROXY_AUTH_UNSUPPORTED"}:
        return "proxy"
    if code.startswith(("ERR_CERT_", "ERR_SSL_")):
        return "tls"
    if code in {"ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED"}:
        return "network"
    if code in {"ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_ABORTED", "ERR_CONNECTION_REFUSED", "ERR_EMPTY_RESPONSE"}:
        return "connection"
    if isinstance(exc, (PlaywrightTimeout, TimeoutError)) or code in {"ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT"}:
        return "timeout"
    if code == "ERR_INVALID_URL":
        return "invalid_url"
    return "navigation"


def navigation_once(page, url: str, *, timeout_ms: int | None = None, attempt: int = 1, attempts: int = 1):
    """保持原始异常类型供重试与认证逻辑处理，附加可安全写入日志的诊断。"""
    target = safe_destination(url)
    started = time.monotonic()
    kwargs = {"timeout": timeout_ms} if timeout_ms is not None else {}
    try:
        response = page.goto(url, wait_until="domcontentloaded", **kwargs)
    except (PlaywrightError, OSError, TimeoutError) as exc:
        match = _ERROR_CODE.search(str(exc))
        code = match.group(1) if match else "TIMEOUT" if isinstance(exc, (PlaywrightTimeout, TimeoutError)) else "NAVIGATION_ERROR"
        event = {
            "kind": "navigation", "outcome": "failed", "stage": "page_load",
            **target, "category": _category(code, exc), "code": code,
            "attempt": attempt, "attempts": attempts,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            **proxy_flags(target["host"]),
        }
        exc.navigation_diagnostic = event
        logger.warning("连接诊断 %s", json.dumps(event, ensure_ascii=False))
        raise
    status = getattr(response, "status", None)
    logger.info("页面导航 %s", json.dumps({
        "kind": "navigation", "outcome": "loaded", **target,
        "http_status": status if isinstance(status, int) else None,
        "attempt": attempt, "attempts": attempts,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }, ensure_ascii=False))
    return response


def failure_diagnostic(exc: BaseException) -> dict | None:
    visited = set()
    current = exc
    attempts = getattr(exc, "attempts", None)
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        event = getattr(current, "navigation_diagnostic", None)
        if isinstance(event, dict):
            result = dict(event)
            if isinstance(attempts, int):
                result.update(attempt=attempts, attempts=attempts)
            return result
        current = getattr(current, "last_error", None) or current.__cause__
    return None


def explain_failure(exc: BaseException) -> str | None:
    event = failure_diagnostic(exc)
    if event is None:
        return None
    category = event["category"]
    descriptions = {
        "dns": "无法解析目标网站地址。请检查 DNS、网络和代理设置。",
        "proxy": "浏览器无法通过代理建立连接。请检查代理是否运行、端口和认证配置是否有效。",
        "tls": "HTTPS 安全连接或证书校验失败。请检查系统时间、证书及代理设置；程序不会关闭证书校验。",
        "connection": "页面加载时连接被关闭、重置或拒绝，可能来自网站或中间网络/代理；仅凭此错误不能确定是哪一方。请在普通浏览器打开同一链接，并检查网络和代理。",
        "network": "网络已断开或在加载时发生变化。请恢复网络连接后重试。",
        "timeout": "页面未在规定时间内加载完成。请检查网络及代理，稍后重试。",
        "invalid_url": "浏览器无法打开此链接，请检查链接格式。",
        "navigation": "浏览器未能加载页面。请在普通浏览器打开同一链接，并查看连接诊断日志。",
        "browser_closed": "浏览器会话已关闭或意外退出，无法继续加载。已保存的数据保留，请重新启动任务；这不等于网站连接或代理故障。",
    }
    configured = category != "browser_closed" and (event.get("environment_proxy_configured") or event.get("windows_proxy_enabled"))
    target = event["host"] + event["path"]
    return (
        descriptions[category]
        + f"\n目标：{target}；错误码：{event['code']}；已尝试 {event['attempt']} 次。"
        + ("检测到本机配置了代理，但这不代表已确定代理有故障。" if configured else "")
    )
