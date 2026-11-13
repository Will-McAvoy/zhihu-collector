"""查看并重试失败的导出和图片。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.repairs import retry_saved_failures, retry_body_failures
from zhihu_collector.config import Settings

_KIND = {"export": "导出", "image": "图片", "body": "正文（需联网）"}


class FailuresWindow(QDialog):
    def __init__(self, database, settings: Settings, parent=None):
        super().__init__(parent)
        self.database = database
        self.settings = settings
        self.controller = TaskController()
        self.worker = None
        self.setWindowTitle("失败项")
        self.resize(860, 480)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["类型", "标题", "作者", "对象", "原因"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.status = QLabel("图片/导出重试不重新采集正文；正文恢复需联网访问原任务。")
        self.progress = QProgressBar()
        retry = QPushButton("重试图片和导出")
        self.body_retry_button = QPushButton("重新获取失败正文（联网）")
        self.body_retry_button.clicked.connect(lambda: self._retry(body=True))
        refresh = QPushButton("刷新")
        cancel = QPushButton("取消")
        self.retry_button = retry
        self.cancel_button = cancel
        self.cancel_button.setEnabled(False)
        retry.clicked.connect(lambda: self._retry())
        refresh.clicked.connect(self.reload)
        cancel.clicked.connect(self._cancel)
        buttons = QHBoxLayout()
        buttons.addWidget(retry)
        buttons.addWidget(self.body_retry_button)
        buttons.addWidget(cancel)
        buttons.addWidget(refresh)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.table)
        layout.addWidget(self.progress)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        rows = self.database.list_failures()
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = [
                _KIND.get(row["kind"], row["kind"]),
                row["title"] or "未命名",
                row["author_name"] or "",
                row["detail"] or "",
                row["reason"] or "",
            ]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        if not rows:
            self.status.setText("没有失败项。")

    def _retry(self, body=False) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        self.controller = TaskController()
        output = self.settings.resolved_output_dir()
        self.worker = _RetryWorker(self.database, self.settings, output, self.settings.save_metadata, self.controller, body=body)
        self.worker.progress_changed.connect(self._on_progress)
        self.worker.finished_result.connect(self._on_finished)
        self.retry_button.setEnabled(False)
        self.body_retry_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.worker.start()

    def closeEvent(self, event) -> None:
        if hasattr(self, "worker") and self.worker is not None and self.worker.isRunning():
            self.controller.stop()
            self.status.setText("正在取消并退出...")
            if not getattr(self, "_closing_connected", False):
                self._closing_connected = True
                try:
                    self.worker.finished.connect(self.close)
                except Exception:
                    pass
            if not self.worker.wait(100):
                event.ignore()
                return
        event.accept()

    def reject(self) -> None:
        if hasattr(self, "worker") and self.worker is not None and self.worker.isRunning():
            self.controller.stop()
            self.status.setText("正在取消并退出...")
            if not getattr(self, "_closing_connected", False):
                self._closing_connected = True
                try:
                    self.worker.finished.connect(self.reject)
                except Exception:
                    pass
            if not self.worker.wait(100):
                return
        super().reject()

    def _cancel(self) -> None:
        self.controller.stop()
        self.status.setText("正在取消。已经替换完成的文件会保留。")

    def _on_progress(self, done: int, total: int, title: str) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        self.status.setText(f"{done}/{total} {title}")

    def _on_finished(self, result) -> None:
        self.retry_button.setEnabled(True)
        self.body_retry_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.status.setText(result.message)
        self.reload()
        if getattr(self, "_parent_closing", False) or getattr(self, "_closing_connected", False):
            return
        titles = {
            "success": "全部成功",
            "partial": "部分失败",
            "cancelled": "已取消",
            "needs_login": "需要登录",
        }
        box = QMessageBox.information if result.status in {"success", "cancelled"} else QMessageBox.warning
        box(self, titles.get(result.status, "失败项"), result.message)


class _RetryWorker(QThread):
    progress_changed = Signal(int, int, str)
    finished_result = Signal(object)

    def __init__(self, database, settings, output_dir: Path, save_metadata: bool, controller, body=False):
        super().__init__()
        self.database = database
        self.settings = settings
        self.output_dir = output_dir
        self.save_metadata = save_metadata
        self.body = body
        self.controller = controller

    def run(self) -> None:
        from zhihu_collector.collectors.offline import OfflineResult
        from zhihu_collector.collectors.runtime_lock import BUSY_MESSAGE, GATE

        if not GATE.acquire("retry"):
            self.finished_result.emit(OfflineResult("failed", 0, 0, BUSY_MESSAGE))
            return
        try:
            if self.body:
                result = retry_body_failures(self.database, self.settings, self.controller,
                    on_progress=lambda done, total, title: self.progress_changed.emit(done, total, title))
                self.finished_result.emit(result)
                return
            result = retry_saved_failures(
                self.database,
                self.settings,
                self.output_dir,
                save_metadata=self.save_metadata,
                controller=self.controller,
                on_progress=lambda done, total, title: self.progress_changed.emit(done, total, title),
            )
        except Exception as exc:
            result = OfflineResult("failed", 0, 0, f"重试异常：{exc}")
        finally:
            GATE.release()
        self.finished_result.emit(result)
