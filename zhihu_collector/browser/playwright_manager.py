"""Chromium 会话。默认无头，调试模式才显示窗口。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from threading import Lock
from uuid import uuid4

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, sync_playwright

from zhihu_collector.config import data_dir, runtime_cache_dir
from zhihu_collector.errors import CollectorError
from zhihu_collector.browser.guarded import bounded, GuardedPageObject
from zhihu_collector.browser.process_owner import DriverOwner
from zhihu_collector.browser.owned_temp import ManagedTemp
from zhihu_collector.browser.sync_guard import install

from zhihu_collector.utils.logger import get_logger

logger = get_logger("browser")

_DEFAULT_ARGS = [
    "--lang=zh-CN",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)


_DRIVER_START_LOCK = Lock()
_TEMP_ENV = ("TEMP", "TMP", "TMPDIR")


def _directory_accepts_subdirectories(directory: Path) -> bool:
    # 单次 mkdir：Windows 对某些拒绝访问目录会反复重试 mkdtemp。
    probe = directory / f"zhihu-pw-check-{uuid4().hex}"
    try:
        probe.mkdir()
        probe.rmdir()
        return True
    except OSError:
        return False


def _default_browser_temp_is_writable() -> bool:
    # Node 在 Windows 使用 TEMP、TMP，最后才使用系统临时目录。
    if os.name != "nt":
        return True
    configured = os.environ.get("TEMP") or os.environ.get("TMP")
    if not configured:
        return True  # 没有显式设置时，让 Playwright 自己判断；有错误时仍能回退。
    return _directory_accepts_subdirectories(Path(configured))


def _temp_launch_error(exc: PlaywrightError) -> bool:
    message = str(exc).lower()
    return "mkdtemp" in message and any(
        code in message for code in ("eperm", "eacces", "enoent", "enotdir", "erofs")
    )


def _start_driver(temp_path: Path | None) -> Playwright:
    # 驱动进程在 start() 中复制环境。只在这个短窗口指定环境，结束后恢复。
    # 普通启动也持锁，防止另一个会话意外继承当前会话的临时目录。
    with _DRIVER_START_LOCK:
        if temp_path is None:
            return sync_playwright().start()
        old_values = {key: os.environ.get(key) for key in _TEMP_ENV}
        try:
            os.environ.update({key: str(temp_path) for key in _TEMP_ENV})
            return sync_playwright().start()
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class BrowserSession:
    """一次 Playwright Chromium 会话。

    正常采集使用 headless=True，桌面上不会出现浏览器窗口。
    登录和调试模式使用 headless=False，方便用户自己完成扫码或观察页面。
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        storage_state: Path | None = None,
        slow_mo_ms: int = 0,
        controller=None,
        offline: bool = False,
    ):
        self.controller = controller
        self.offline = offline
        self._owner = None
        self.headless = headless
        self.storage_state = storage_state if storage_state and storage_state.is_file() else None
        self.slow_mo_ms = slow_mo_ms
        self._playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self._temp_dir = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def run_guarded(self, stage, operation, seconds=23, *, cancellable=True):
        return bounded(self, stage, operation, seconds, cancellable=cancellable)

    def abort(self):
        """Watchdog thread: terminate owned handles; wake futures on their own loop."""
        if self._owner is not None:
            self._owner.terminate()
        connection = getattr(getattr(self._playwright, "_impl_obj", None), "_connection", None)
        if connection is None:
            return

        def disconnect():
            # Pipe EOF alone may not wake an in-flight sync call during shutdown.
            if not connection._closed_error:
                connection.cleanup("本任务浏览器因停止请求或操作超时而退出")
            transport = connection._transport
            if not transport.on_error_future.done():
                transport.on_error_future.set_exception(PlaywrightError("本任务浏览器已限时结束"))
                transport.on_error_future.exception()
            transport.request_stop()

        try:
            connection._loop.call_soon_threadsafe(disconnect)
        except RuntimeError:
            pass  # Loop already closed; OS handles have still been reclaimed.

    def start(self) -> None:
        logger.info(
            "启动 Chromium（%s）",
            "显示窗口" if not self.headless else "后台无头",
        )
        launch_kwargs: dict = {
            "headless": self.headless,
            # 使用完整 Chromium 的无头模式，而不是更瘦的 headless shell。
            # shell 没有窗口，但知乎经常把它当成异常环境。这里仍然是 headless=True。
            "channel": "chrome-for-testing",
            "args": list(_DEFAULT_ARGS),
        }
        if self.slow_mo_ms > 0:
            launch_kwargs["slow_mo"] = self.slow_mo_ms
        context_kwargs: dict = {
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "user_agent": _DEFAULT_USER_AGENT,
            "viewport": {"width": 1280, "height": 900},
        }
        if self.storage_state is not None:
            context_kwargs["storage_state"] = str(self.storage_state)
            logger.info("已加载登录状态文件")
        else:
            logger.info("未找到登录状态，将以未登录会话访问公开页面")
        if self.offline:
            context_kwargs['offline'] = True
        try:
            # Always track ownership, including on machines with a writable TEMP.
            # The driver receives this environment only during its own startup.
            try:
                self._launch_with_local_temp(launch_kwargs)
            except CollectorError as exc:
                if not isinstance(exc.__cause__, PlaywrightError) or not _temp_launch_error(exc.__cause__):
                    raise
                logger.warning('浏览器临时目录启动失败，回收驱动后更换目录重试一次')
                self.close()
                self._launch_with_local_temp(launch_kwargs)
            assert self.browser is not None
            self.context = self.run_guarded("初始化浏览器上下文", lambda: self.browser.new_context(**context_kwargs), 30)
            install(self.context)
            if not self.offline:
                self.run_guarded("初始化页面脚本", lambda: self.context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"), 10)
            self.context.set_default_navigation_timeout(45_000)
            self.context.set_default_timeout(20_000)
        except Exception:
            try:
                self.close()
            except Exception:
                logger.exception("清理未能启动的 Chromium 会话失败")
            raise

    def _launch(self, launch_kwargs: dict, temp_path: Path | None = None) -> None:
        self._playwright = _start_driver(temp_path)
        process = getattr(getattr(getattr(getattr(self._playwright, "_impl_obj", None), "_connection", None), "_transport", None), "_proc", None)
        if process is not None and isinstance(process.pid, int):
            self._owner = DriverOwner(process)
        self.browser = self.run_guarded("启动 Chromium", lambda: self._playwright.chromium.launch(**launch_kwargs, timeout=30000), 35)
        install(self.browser)
        if self._owner is not None:
            self._owner.capture()

    def _launch_with_local_temp(self, launch_kwargs: dict) -> None:
        directory = runtime_cache_dir(data_dir()) / "browser-temp"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if not _directory_accepts_subdirectories(directory):
                raise PermissionError(directory)
            self._temp_dir = ManagedTemp(directory, 'browser')
        except OSError as exc:
            raise CollectorError(f"Chromium 无法创建临时目录：请检查系统临时目录和应用目录的写入权限（{directory}）。") from exc
        try:
            self._launch(launch_kwargs, Path(self._temp_dir.name).resolve())
        except PlaywrightError as exc:
            if _temp_launch_error(exc):
                raise CollectorError(f"Chromium 无法在应用目录创建临时文件：{directory}。请检查目录权限。") from exc
            raise
        logger.info("Chromium 使用受管理的应用临时目录：%s", directory)

    def new_page(self) -> Page:
        if self.context is None:
            raise RuntimeError("浏览器会话尚未启动")
        return GuardedPageObject(self.run_guarded("创建页面", self.context.new_page, 20), self)

    def save_storage_state(self, path: Path) -> None:
        if self.context is None:
            raise RuntimeError("浏览器会话尚未启动")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        self.context.storage_state(path=str(temporary))
        temporary.replace(path)
        logger.info("登录状态已保存到 %s", path)

    def cookie_names(self) -> list[str]:
        if self.context is None:
            return []
        return sorted({item["name"] for item in self.context.cookies("https://www.zhihu.com")})

    def close(self) -> None:
        # Retain late-created renderers before their parents exit. Capturing only
        # after driver shutdown can no longer discover these descendants.
        if self._owner is not None:
            self._owner.capture()
        if self.controller is not None and self.controller.stopped:
            # Auth state is saved explicitly elsewhere. On cancellation there
            # is no reason to wait for web tabs to complete graceful shutdown.
            self.abort()
            self.context = None
            self.browser = None
        try:
            try:
                if self.browser is not None:
                    # Browser.close already closes its contexts. Separate context
                    # and browser RPCs created two independent shutdown waits.
                    self.run_guarded("关闭浏览器", self.browser.close, 4, cancellable=False)
            finally:
                self.context = None
                self.browser = None
                if self._playwright is not None:
                    playwright = self._playwright
                    try:
                        self.run_guarded("关闭驱动", playwright.stop, 3, cancellable=False)
                    finally:
                        self._playwright = None
        except Exception:
            logger.warning("浏览器关闭未正常完成，已按限时回收本会话进程", exc_info=True)
            if self._owner is not None:
                self._owner.terminate()
        finally:
            if self._owner is not None:
                self._owner.terminate()
                self._owner.release()
                self._owner = None
            if self._temp_dir is not None:
                temp_dir, self._temp_dir = self._temp_dir, None
                try:
                    temp_dir.cleanup()
                except OSError:
                    logger.warning("无法清理 Chromium 临时目录：%s", temp_dir.name, exc_info=True)
            logger.info("Chromium 已关闭")
