"""采集节奏、调试模式和路径设置。"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from zhihu_collector.config import Settings


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(560, 420)
        self.headful = QCheckBox("显示浏览器窗口（调试模式）")
        self.headful.setChecked(settings.headful_debug)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 8)
        self.concurrency.setValue(settings.image_concurrency)
        self.interval = QSpinBox()
        self.interval.setRange(500, 20000)
        self.interval.setSingleStep(100)
        self.interval.setSuffix(" 毫秒")
        self.interval.setValue(settings.page_interval_ms)
        self.retries = QSpinBox()
        self.retries.setRange(1, 8)
        self.retries.setValue(settings.retry_count)
        self.filename = QLineEdit(settings.filename_rule)
        self.database_path = QLineEdit(settings.database_path)
        self.output_dir = QLineEdit(settings.output_dir)
        form = QFormLayout()
        form.addRow("调试模式", self.headful)
        form.addRow("图片下载并发数", self.concurrency)
        form.addRow("页面加载间隔", self.interval)
        form.addRow("失败重试次数", self.retries)
        form.addRow("导出文件命名规则", self.filename)
        form.addRow("数据库位置", self._browse_row(self.database_path, file_mode=True))
        form.addRow("默认输出目录", self._browse_row(self.output_dir, file_mode=False))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self._original = settings

    def result_settings(self) -> Settings:
        settings = replace(self._original)
        settings.headful_debug = self.headful.isChecked()
        settings.image_concurrency = self.concurrency.value()
        settings.page_interval_ms = self.interval.value()
        settings.request_interval_ms = max(500, self.interval.value() - 200)
        settings.retry_count = self.retries.value()
        settings.filename_rule = self.filename.text().strip() or "{index:04d}-{title}"
        settings.database_path = self.database_path.text().strip()
        settings.output_dir = self.output_dir.text().strip()
        return settings

    def _browse_row(self, edit: QLineEdit, *, file_mode: bool):
        button = QPushButton("浏览…")

        def choose() -> None:
            if file_mode:
                path, _ = QFileDialog.getSaveFileName(self, "数据库位置", edit.text() or "zhihu.db", "SQLite (*.db)")
            else:
                path = QFileDialog.getExistingDirectory(self, "目录", edit.text())
            if path:
                edit.setText(path)

        button.clicked.connect(choose)
        row = QHBoxLayout()
        row.addWidget(edit)
        row.addWidget(button)
        return row
