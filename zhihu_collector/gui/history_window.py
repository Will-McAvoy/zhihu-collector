"""任务历史：继续、更新、打开目录、删除记录。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from zhihu_collector.collectors.service import CollectRequest

_STATUS = {
    "running": "进行中",
    "completed": "已完成",
    "interrupted": "已中断",
    "failed": "失败",
    "paused": "已暂停",
    "needs_login": "需要登录",
    "needs_verification": "需要验证",
    "partial": "部分完成",
    "unconfirmed": "完整性未确认",
    "awaiting_confirmation": "等待确认范围",
}
_TYPES = {
    "user_answers": "答主回答",
    "user_pins": "答主想法",
    "user_all": "答主全部内容",
    "question_answers": "问题全部回答",
    "collection_answers": "收藏夹回答",
    "single_answer": "单篇回答",
}


class HistoryWindow(QDialog):
    def __init__(self, database, on_start, parent=None):
        super().__init__(parent)
        self.database = database
        self.on_start = on_start
        self.setWindowTitle("任务历史")
        self.resize(860, 420)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["目标", "任务类型", "时间", "数量", "状态", "输出目录"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        resume = QPushButton("继续任务")
        update = QPushButton("更新内容库")
        export = QPushButton("更新并导出")
        save_source = QPushButton("加入常用来源")
        open_dir = QPushButton("打开输出目录")
        delete = QPushButton("删除任务记录")
        resume.clicked.connect(lambda: self._start(update_only=False, execution_intent=""))
        update.clicked.connect(lambda: self._start(update_only=False, execution_intent="library"))
        export.clicked.connect(lambda: self._start(update_only=False, execution_intent="export"))
        save_source.clicked.connect(self._save_source)
        open_dir.clicked.connect(self._open)
        delete.clicked.connect(self._delete)
        buttons = QHBoxLayout()
        for button in (resume, update, export, save_source, open_dir, delete):
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("继续任务会跳过已经保存且没有变化的内容。删除只去掉这条记录，不删除已下载文件。"))
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        rows = self.database.list_tasks()
        self.table.setRowCount(len(rows))
        self._rows = rows
        for index, row in enumerate(rows):
            values = [
                row["target_url"] or "",
                _TYPES.get(row["task_type"], row["task_type"] or ""),
                row["updated_at"] or row["created_at"] or "",
                f"{row['total_completed'] or 0}/{row['total_found'] or 0}",
                _STATUS.get(row["status"], row["status"] or ""),
                row["output_dir"] or "",
            ]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))

    def _selected(self):
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            QMessageBox.information(self, "任务历史", "请先选择一条任务。")
            return None
        return self._rows[indexes[0].row()]

    def _start(self, *, update_only: bool, execution_intent: str = "") -> None:
        row = self._selected()
        if row is None:
            return
        try:
            options = json.loads(row["options_json"] or "{}")
        except json.JSONDecodeError:
            options = {}
        if execution_intent == "" and "execution_intent" not in options:
            intent = ""
        else:
            intent = execution_intent or str(options.get("execution_intent") or "")
        request = CollectRequest(
            url=options.get("url") or row["target_url"],
            mode=options.get("mode") or "auto",
            output_dir=options.get("output_dir") or row["output_dir"] or "",
            download_images=bool(options.get("download_images", True)),
            save_metadata=bool(options.get("save_metadata", True)),
            export_markdown=bool(options.get("export_markdown", True)),
            export_docx=bool(options.get("export_docx", True)),
            export_pdf=bool(options.get("export_pdf", True)),
            export_separate=bool(options.get("export_separate", True)),
            export_merged=bool(options.get("export_merged", False)),
            update_only=update_only if intent == "" else False,
            execution_intent=intent,
            task_id=int(row["id"]) if intent == "" else None,
        )
        self.on_start(request)
        self.accept()

    def _save_source(self) -> None:
        from zhihu_collector.database import catalog
        from zhihu_collector.utils.batch_targets import history_source_candidates

        row = self._selected()
        if row is None:
            return
        matches = [item for item in history_source_candidates(self.database) if int(item["task_id"]) == int(row["id"])]
        if not matches:
            QMessageBox.information(self, "加入常用来源", "这条历史记录还不能识别成稳定来源。")
            return
        candidate = matches[0]
        try:
            options = json.loads(candidate["options_json"] or "{}")
        except json.JSONDecodeError:
            options = {}
        catalog.save_source(
            self.database,
            display_name=candidate["canonical_url"],
            canonical_url=candidate["canonical_url"],
            target_kind=candidate["target_kind"],
            identity_key=candidate["identity_key"],
            collect_mode=candidate["collect_mode"],
            preset_id=str(options.get("preset_id") or ""),
            preset_snapshot=options,
        )
        QMessageBox.information(self, "加入常用来源", "已加入常用来源。已有正文会按稳定身份关联，不会因此重新下载。")

    def _open(self) -> None:
        row = self._selected()
        if row is None:
            return
        path = Path(row["output_dir"] or "")
        if not path.exists():
            QMessageBox.warning(self, "任务历史", "输出目录不存在。")
            return
        os.startfile(path)

    def _delete(self) -> None:
        row = self._selected()
        if row is None:
            return
        answer = QMessageBox.question(self, "删除任务记录", "只删除这条任务记录，已保存的内容和文件都会保留。")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.database.delete_task(int(row["id"]))
        self.reload()
