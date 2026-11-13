"""采集和登录都放在后台线程，避免界面卡住。"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from zhihu_collector.browser.auth_manager import AuthManager
from zhihu_collector.browser.navigation import explain_failure
from zhihu_collector.collectors.service import CollectRequest, CollectionService
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.runtime_lock import BUSY_MESSAGE, GATE
from zhihu_collector.errors import CollectionScopeReady


class CollectWorker(QThread):
    progress_changed = Signal(object)
    log_line = Signal(str)
    succeeded = Signal(str)
    failed = Signal(str)
    scope_ready = Signal(object)

    def __init__(self, settings: Settings, database: Database, request: CollectRequest, controller: TaskController):
        super().__init__()
        self.settings = settings
        self.database = database
        self.request = request
        self.controller = controller

    def run(self) -> None:
        if not GATE.acquire("collect"):
            self.failed.emit(BUSY_MESSAGE)
            return
        service = CollectionService(
            self.settings,
            self.database,
            self.controller,
            on_progress=self.progress_changed.emit,
            on_log=self.log_line.emit,
        )
        try:
            output = service.run(self.request)
        except CollectionScopeReady as exc:
            self.scope_ready.emit(exc.plan)
            return
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        finally:
            GATE.release()
        message = service.progress.message or "搜集完成"
        self.succeeded.emit(f"{message}\n{output}" if output else message)


class LoginWorker(QThread):
    finished_status = Signal(object)

    def __init__(self, auth: AuthManager, controller: TaskController | None = None):
        super().__init__()
        self.auth = auth
        self.controller = controller or TaskController()

    def cancel(self) -> None:
        self.controller.stop()

    def run(self) -> None:
        try:
            status = self.auth.login_interactive(controller=self.controller)
        except Exception as exc:
            from zhihu_collector.browser.auth_manager import AuthStatus
            from zhihu_collector.utils.time_utils import now_iso

            status = AuthStatus(logged_in=False, message=explain_failure(exc) or f"登录窗口异常：{exc}", checked_at=now_iso())
        self.finished_status.emit(status)
