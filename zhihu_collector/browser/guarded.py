"""Bound synchronous browser calls without invoking Playwright on another thread."""
from __future__ import annotations

import threading
import time

from playwright.sync_api import Frame, Locator, Page
from zhihu_collector.errors import CollectorError, TaskStopped


class BrowserOperationTimeout(CollectorError):
    pass


def bounded(session, stage, operation, seconds, *, cancellable=True):
    controller = session.controller if cancellable else None
    if controller:
        controller.checkpoint()
    finished = threading.Event()
    reason = []
    started = time.monotonic()

    def watch():
        while not finished.wait(0.1):
            stopped = controller is not None and controller.stopped
            if stopped or time.monotonic() - started >= seconds:
                reason.append("stopped" if stopped else "timeout")
                abort = getattr(session, "abort", None)
                if callable(abort):
                    abort()
                elif session._owner is not None:
                    session._owner.terminate()
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        return operation()
    except Exception:
        if not reason:
            raise
    finally:
        finished.set()
        watcher.join(timeout=2)
        if reason:
            if reason[0] == "stopped":
                raise TaskStopped()
            raise BrowserOperationTimeout(f"浏览器操作超时：{stage}（限时 {seconds:g} 秒）。已结束本任务浏览器，已保存的数据保留，可重试。")


class GuardedPageObject:
    def __init__(self, raw, session):
        from zhihu_collector.browser.sync_guard import install
        install(raw)
        self._raw = raw
        self._session = session

    def run_guarded(self, stage, operation, seconds=15):
        return self._session.run_guarded(stage, operation, seconds)

    def _wrap(self, value):
        return GuardedPageObject(value, self._session) if isinstance(value, (Page, Frame, Locator)) else value

    def __getattr__(self, name):
        value = getattr(self._raw, name)
        if not callable(value):
            return self._wrap(value)
        if name in {"on", "once", "remove_listener", "remove_all_listeners"}:
            return value

        def call(*args, **kwargs):
            seconds = float(kwargs.get("timeout") or (45000 if name == "goto" else 20000)) / 1000 + 3
            if name == "wait_for_timeout" and args:
                seconds = float(args[0]) / 1000 + 5
            if name == "pdf":
                seconds = 60
            return self._wrap(self.run_guarded(name, lambda: value(*args, **kwargs), seconds))
        return call
