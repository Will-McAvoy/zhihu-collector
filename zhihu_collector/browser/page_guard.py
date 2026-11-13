"""判断当前页是正常内容、登录墙，还是需要用户手动处理的安全验证。"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from zhihu_collector.errors import LoginExpiredError, VerificationRequiredError
from zhihu_collector.utils.urls import is_zhihu_host

_VERIFICATION_TEXT = (
    "安全验证",
    "完成验证后继续",
    "请拖动滑块",
    "异常访问",
    "请输入验证码",
    "系统繁忙",
    "限制本次访问",
    "请求存在异常",
    "请求参数异常",
    "40362",
    "升级客户端",
)
_VERIFICATION_ROUTES = {"captcha", "unhuman", "anticrawl", "verification"}
_LOGIN_ERROR_HINTS = ("请先登录", "请您登录", "未登录", "需要登录", "登录后", "登录已失效", "登录状态失效", "登录过期", "登录知乎")


def _verification_route(url: str) -> bool:
    parts = urlparse(url).path.lower().strip("/").split("/")
    return parts[0] in _VERIFICATION_ROUTES or (
        len(parts) > 1 and parts[0] in {"account", "security"} and parts[1] in _VERIFICATION_ROUTES
    )


def inspect_page(page, *, found_content: int, network_errors: list[dict] | None = None) -> None:
    """地址跳转和本轮新的列表接口错误优先于页面上残留的旧内容。"""
    _raise_for_navigation(page)
    _raise_for_network_errors(network_errors)
    if found_content > 0:
        return
    text = _body_preview(page)
    if any(hint in text for hint in _VERIFICATION_TEXT):
        raise VerificationRequiredError()
    if "请您登录" in text or ("登录后" in text and ("回答" in text or "更多" in text or "继续" in text)):
        raise LoginExpiredError("知乎要求登录后才能查看该内容。请先登录知乎。")


def _raise_for_navigation(page) -> None:
    current = getattr(page, "url", "")
    if not isinstance(current, str):
        return
    url = current.lower()
    parsed = urlparse(url)
    if parsed.path.strip("/").split("/")[0] in {"signin", "signup"}:
        raise LoginExpiredError("当前内容需要登录后才能查看。请先登录知乎。")
    verification = _verification_route(url)
    if verification and ("true" in parse_qs(parsed.query).get("need_login", []) or "请您登录" in _body_preview(page)):
        raise LoginExpiredError(
            "知乎要求登录后才能继续搜集。请点击“登录知乎”，在打开的窗口中自行完成登录。程序不会自动处理验证。"
        )
    if verification:
        raise VerificationRequiredError()


def _raise_for_network_errors(network_errors: list[dict] | None) -> None:
    for error in network_errors or []:
        url = str(error.get("url") or "")
        message = str(error.get("message") or "")
        status = int(error.get("status") or 0)
        if not _is_listing_error(url):
            continue
        code = str(error.get("code") or "")
        if status == 401 or code == "401" or any(hint in message for hint in _LOGIN_ERROR_HINTS):
            raise LoginExpiredError()
        if code == "40362" or _verification_route(url) or any(hint in message for hint in _VERIFICATION_TEXT):
            raise VerificationRequiredError()
        if status in {403, 429}:
            raise VerificationRequiredError(f"知乎接口访问受限（状态码 {status}）：{message or '请在浏览器中验证后再试'}")


def _is_listing_error(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc and not is_zhihu_host(parsed.hostname):
        return False
    parts = parsed.path.rstrip("/").split("/")
    if len(parts) < 5 or parts[1] != "api":
        return False
    if len(parts) == 7 and parts[3:5] == ["v2", "pins"] and parts[-1] == "moments":
        return True
    return (parts[3] in {"collections", "favlists"}
            or parts[3] in {"members", "questions"} and parts[-1] in {"answers", "pins", "feeds"})


def _body_preview(page) -> str:
    try:
        text = page.inner_text("body")
    except Exception:
        return ""
    return (text or "")[:2500]
