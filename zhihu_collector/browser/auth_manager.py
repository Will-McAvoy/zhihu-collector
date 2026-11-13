"""知乎登录状态。用户自己完成扫码或验证码，程序只负责保存浏览器状态。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from zhihu_collector.browser.playwright_manager import BrowserSession
from zhihu_collector.browser.navigation import navigation_once
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.config import default_storage_state_path
from zhihu_collector.utils.logger import get_logger
from zhihu_collector.utils.time_utils import now_iso

logger = get_logger("auth")

LOGIN_URL = "https://www.zhihu.com/signin"
HOME_URL = "https://www.zhihu.com/"
AUTH_COOKIE = "z_c0"


@dataclass
class AuthStatus:
    logged_in: bool
    message: str
    user_name: str = ""
    checked_at: str = ""


def cookies_indicate_login(cookies: list[dict]) -> bool:
    for cookie in cookies:
        if cookie.get("name") == AUTH_COOKIE and cookie.get("value"):
            return True
    return False


def _page_confirms_login(page, cookies: list[dict]) -> bool:
    """A retained cookie can be rejected by the server before its expiry date."""
    if not cookies_indicate_login(cookies):
        return False
    path = urlsplit(page.url).path.strip('/').split('/')[0].lower()
    if path in {'signin', 'signup', 'captcha', 'unhuman', 'anticrawl', 'verification'}:
        return False
    return page.evaluate("""() => !!document.querySelector(
        '.AppHeader-profile img.AppHeader-profileAvatar, .AppHeader-profileAvatar'
    )""") is True


class AuthManager:
    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path or default_storage_state_path()

    def has_saved_state(self) -> bool:
        return self.state_path.is_file()

    def login_interactive(self, timeout_sec: int = 180, controller: TaskController | None = None) -> AuthStatus:
        """打开可见浏览器，等待用户手动登录，成功后保存 storage state。"""
        logger.info("打开可见浏览器，等待用户登录知乎")
        with BrowserSession(headless=False, storage_state=self.state_path if self.has_saved_state() else None, controller=controller) as session:
            page = session.new_page()
            navigation_once(page, LOGIN_URL)
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if controller is not None and controller.is_stopped():
                    logger.info("用户取消登录或窗口已关闭")
                    return AuthStatus(
                        logged_in=False,
                        message="登录已取消。",
                        checked_at=now_iso(),
                    )
                cookies = session.context.cookies("https://www.zhihu.com") if session.context else []
                if _page_confirms_login(page, cookies):
                    page.wait_for_timeout(800)
                    user_name = _read_logged_in_name(page)
                    session.save_storage_state(self.state_path)
                    message = "登录状态已保存。"
                    if user_name:
                        message = f"已登录{user_name}，登录状态已保存。"
                    logger.info(message)
                    return AuthStatus(
                        logged_in=True,
                        message=message,
                        user_name=user_name,
                        checked_at=now_iso(),
                    )
                for _ in range(10):
                    if controller is not None and controller.is_stopped():
                        logger.info("用户取消登录或窗口已关闭")
                        return AuthStatus(
                            logged_in=False,
                            message="登录已取消。",
                            checked_at=now_iso(),
                        )
                    page.wait_for_timeout(100)
        logger.warning("等待登录超时")
        return AuthStatus(
            logged_in=False,
            message="等待登录超时。请重新点击“登录知乎”，并在打开的窗口中完成登录。",
            checked_at=now_iso(),
        )

    def validate(self, *, headless: bool = True) -> AuthStatus:
        """打开知乎首页，同时核对 Cookie 和网页实际登录状态。"""
        if not self.has_saved_state():
            return AuthStatus(
                logged_in=False,
                message="尚未登录知乎。部分公开内容仍可尝试搜集，不完整时请先登录。",
                checked_at=now_iso(),
            )
        with BrowserSession(headless=headless, storage_state=self.state_path) as session:
            page = session.new_page()
            navigation_once(page, HOME_URL)
            page.wait_for_timeout(1500)
            cookies = session.context.cookies("https://www.zhihu.com") if session.context else []
            if _page_confirms_login(page, cookies):
                user_name = _read_logged_in_name(page)
                return AuthStatus(
                    logged_in=True,
                    message="登录状态有效。" if not user_name else f"登录状态有效：{user_name}",
                    user_name=user_name,
                    checked_at=now_iso(),
                )
        return AuthStatus(
            logged_in=False,
            message="知乎网页未确认有效登录。请重新登录；若页面要求安全验证，请先在浏览器中完成验证。",
            checked_at=now_iso(),
        )


def _read_logged_in_name(page) -> str:
    try:
        name = page.evaluate(
            """() => {
                const node = document.querySelector(
                    '.AppHeader-profile .AppHeader-menuItem, .ProfileHeader-name, img.AppHeader-profileAvatar'
                );
                if (!node) return '';
                return (node.getAttribute('aria-label') || node.textContent || node.getAttribute('alt') || '').trim();
            }"""
        )
    except Exception:
        return ""
    if not isinstance(name, str):
        return ""
    return name.replace("个人主页", "").strip()[:40]
