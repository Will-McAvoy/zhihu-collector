"""导出前范围与计划确认对话框。"""

from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from zhihu_collector.export_plan import ExportPlan


class ConfirmPlanDialog(QDialog):
    def __init__(self, plan: ExportPlan, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.confirmed = False
        self.setWindowTitle("确认导出范围与选项" if plan.is_exact else "确认采集计划")
        self.resize(680, 560)
        self.setFont(QFont("Microsoft YaHei", 9))
        self._build()

    def exec(self) -> int:
        return super().exec()

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        # 来源与范围信息
        info_group = QGroupBox("任务范围与来源")
        info_form = QFormLayout(info_group)

        source_desc = self.plan.source_title
        if self.plan.source_id:
            source_desc += f" (ID: {self.plan.source_id})"
        info_form.addRow("来源对象：", QLabel(source_desc or "知乎公开页面"))

        if self.plan.is_exact:
            scope_desc = f"精确选择 {self.plan.exact_count} 篇内容"
            if self.plan.merged_filename:
                info_form.addRow("合并文件名：", QLabel(self.plan.merged_filename))
            if not self.plan.target_count_unknown:
                info_form.addRow("预计生成文件数：", QLabel(str(self.plan.expected_file_count)))
                info_form.addRow("已存在目标文件：", QLabel(str(self.plan.existing_target_count)))
        else:
            if self.plan.max_items > 0:
                scope_desc = f"数量待发现，最多采集 {self.plan.max_items} 篇"
            else:
                scope_desc = "数量待发现，全量采集（不限篇数）"
            info_form.addRow("预计生成文件数：", QLabel("待发现" if self.plan.target_count_unknown else str(self.plan.expected_file_count)))
        self.scope_label = QLabel(scope_desc)
        scope_label = self.scope_label
        scope_label.setStyleSheet("font-weight: bold; color: #1a73e8;")
        info_form.addRow("内容范围：", scope_label)

        # 格式与选项
        formats = []
        if self.plan.export_markdown:
            formats.append("Markdown")
        if self.plan.export_docx:
            formats.append("Word")
        if self.plan.export_pdf:
            formats.append("PDF")
        info_form.addRow("导出格式：", QLabel(", ".join(formats) or "无"))

        styles = []
        if self.plan.export_separate:
            styles.append("每篇单独文件")
        if self.plan.export_merged:
            styles.append("合并电子书")
        info_form.addRow("输出方式：", QLabel(", ".join(styles) or "无"))

        options = []
        if self.plan.download_images:
            options.append("下载配图")
        if self.plan.save_metadata:
            options.append("保存元数据")
        info_form.addRow("附加选项：", QLabel(", ".join(options) or "无"))

        layout.addWidget(info_group)

        # 若是精确范围，展示清单表格
        if self.plan.is_exact and self.plan.member_items:
            table_box = QGroupBox(f"内容清单 ({len(self.plan.member_items)} 篇)")
            table_layout = QVBoxLayout(table_box)
            self.table = QTableWidget(len(self.plan.member_items), 4)
            self.table.setHorizontalHeaderLabels(["序号", "标题", "作者", "ID / 状态"])
            self.table.horizontalHeader().setStretchLastSection(True)
            self.table.setColumnWidth(0, 45)
            self.table.setColumnWidth(1, 300)
            self.table.setColumnWidth(2, 120)

            for idx, item in enumerate(self.plan.member_items):
                num_item = QTableWidgetItem(str(idx + 1))
                num_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                title_item = QTableWidgetItem(str(item.get("title") or "未命名"))
                author_item = QTableWidgetItem(str(item.get("author") or "未知作者"))
                status_text = str(item.get("status") or item.get("id") or "")
                status_item = QTableWidgetItem(status_text)

                for cell in (num_item, title_item, author_item, status_item):
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)

                self.table.setItem(idx, 0, num_item)
                self.table.setItem(idx, 1, title_item)
                self.table.setItem(idx, 2, author_item)
                self.table.setItem(idx, 3, status_item)

            table_layout.addWidget(self.table)
            layout.addWidget(table_box)

        # 路径与冲突设置
        output_group = QGroupBox("输出位置与冲突处理")
        output_layout = QVBoxLayout(output_group)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self.plan.output_root)
        self.path_edit.setReadOnly(True)
        open_folder_btn = QPushButton("打开目录")
        open_folder_btn.clicked.connect(self._open_folder)
        path_row.addWidget(self.path_edit)
        path_row.addWidget(open_folder_btn)
        output_layout.addLayout(path_row)

        conflict_tip = QLabel("当输出目录中已存在同名目标文件时的处理策略：")
        output_layout.addWidget(conflict_tip)

        self.conflict_group = QButtonGroup(self)
        self.radio_new_copy = QRadioButton("生成新副本（推荐，在文件名后添加 _副本 标记，保护已有文件）")
        self.radio_overwrite = QRadioButton("覆盖现有文件（直接覆盖同名文件）")
        self.radio_skip = QRadioButton("跳过已有文件（保留现有文件，不再重复生成）")

        if self.plan.conflict_policy == "overwrite":
            self.radio_overwrite.setChecked(True)
        elif self.plan.conflict_policy == "skip":
            self.radio_skip.setChecked(True)
        else:
            self.radio_new_copy.setChecked(True)

        self.conflict_group.addButton(self.radio_new_copy)
        self.conflict_group.addButton(self.radio_overwrite)
        self.conflict_group.addButton(self.radio_skip)

        output_layout.addWidget(self.radio_new_copy)
        output_layout.addWidget(self.radio_overwrite)
        output_layout.addWidget(self.radio_skip)

        layout.addWidget(output_group)

        # 底部操作按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)

        action_text = f"确认导出 {self.plan.exact_count} 篇" if self.plan.is_exact else "确认开始采集"
        confirm_btn = QPushButton(action_text)
        confirm_btn.setStyleSheet(
            "background-color: #0066cc; color: white; font-weight: bold; padding: 6px 16px;"
        )
        confirm_btn.clicked.connect(self._on_confirm)

        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        layout.addLayout(btn_layout)

    def _open_folder(self) -> None:
        path = Path(self.plan.output_root).resolve()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_confirm(self) -> None:
        if self.radio_overwrite.isChecked():
            self.plan.conflict_policy = "overwrite"
        elif self.radio_skip.isChecked():
            self.plan.conflict_policy = "skip"
        else:
            self.plan.conflict_policy = "new_copy"

        self.confirmed = True
        self.accept()
