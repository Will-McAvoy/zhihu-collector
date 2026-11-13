"""查看 logs/app.log。"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QPlainTextEdit, QPushButton, QVBoxLayout

from zhihu_collector.config import log_dir


class LogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("运行日志")
        self.resize(760, 480)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.reload)
        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addWidget(refresh)
        self.reload()

    def reload(self) -> None:
        path = log_dir() / "app.log"
        if not path.is_file():
            self.view.setPlainText("还没有日志。")
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        self.view.setPlainText(text[-200_000:])
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())
