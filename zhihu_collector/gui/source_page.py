"""常用来源。来源保存目标，配置预设保存怎么采集。"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zhihu_collector.database import catalog
from zhihu_collector.utils.batch_targets import history_source_candidates, identity_for
from zhihu_collector.utils.urls import parse_zhihu_url

_MODE_LABELS = {
    "answers": "回答",
    "pins": "想法",
    "all": "全部内容",
    "question": "问题",
    "single_answer": "单篇回答",
    "collection": "收藏夹",
}
_STATUS_LABELS = {
    "completed": "成功",
    "partial": "部分完成",
    "unconfirmed": "完整性未确认",
    "failed": "失败",
    "needs_login": "需要登录",
    "needs_verify": "需要验证",
    "interrupted": "已中断",
    "awaiting_confirm": "等待确认",
    "": "尚未检查",
}


class SourcePage(QWidget):
    def __init__(self, database, preset_manager, on_enqueue, on_open_library, parent=None):
        super().__init__(parent)
        self.database = database
        self.preset_manager = preset_manager
        self.on_enqueue = on_enqueue
        self.on_open_library = on_open_library
        self._rows = []
        self._build()
        self.reload()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("常用来源")
        title.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        hint = QLabel("保存要采集的作者、问题或收藏夹。移除来源只去掉快捷入口，不删除正文、图片和历史。")
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        layout.addWidget(title)
        layout.addWidget(hint)
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("按名称或链接搜索")
        self.search_edit.returnPressed.connect(self.reload)
        search_button = QPushButton("搜索")
        search_button.clicked.connect(self.reload)
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("按最近尝试时间", "attempt_desc")
        self.sort_combo.addItem("按自定义顺序", "order_asc")
        self.sort_combo.addItem("按名称", "name_asc")
        self.sort_combo.currentIndexChanged.connect(self.reload)
        search_row.addWidget(self.search_edit, 3)
        search_row.addWidget(search_button)
        search_row.addWidget(QLabel("排序："))
        search_row.addWidget(self.sort_combo, 1)
        layout.addLayout(search_row)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["选择", "名称", "类型/范围", "本地内容", "最近检查", "最近成功", "最近状态"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table)
        self.empty_label = QLabel("尚无常用来源。可以添加链接，或从历史记录里勾选后导入。")
        self.empty_label.setProperty("role", "hint")
        layout.addWidget(self.empty_label)
        buttons = QHBoxLayout()
        add_button = QPushButton("添加链接")
        import_button = QPushButton("从历史导入")
        rename_button = QPushButton("改名称")
        edit_opt_button = QPushButton("编辑默认选项")
        up_button = QPushButton("上移")
        down_button = QPushButton("下移")
        update_button = QPushButton("更新内容库")
        update_button.setProperty("role", "primary")
        export_button = QPushButton("更新并导出")
        open_button = QPushButton("查看该来源内容")
        web_button = QPushButton("打开原网页")
        remove_button = QPushButton("移除来源")
        add_button.clicked.connect(self._add)
        import_button.clicked.connect(self._import_history)
        rename_button.clicked.connect(self._rename)
        edit_opt_button.clicked.connect(self._edit_options)
        up_button.clicked.connect(lambda: self._move(-1))
        down_button.clicked.connect(lambda: self._move(1))
        update_button.clicked.connect(lambda: self._enqueue("library"))
        export_button.clicked.connect(lambda: self._enqueue("export"))
        open_button.clicked.connect(self._open_library)
        web_button.clicked.connect(self._open_web)
        remove_button.clicked.connect(self._remove)
        for button in (
            add_button, import_button, rename_button, edit_opt_button,
            up_button, down_button, update_button, export_button,
            open_button, web_button, remove_button,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def reload(self) -> None:
        keyword = self.search_edit.text().strip().lower()
        rows = []
        for source in catalog.list_sources(self.database):
            haystack = f"{source['display_name']} {source['canonical_url']}".lower()
            if keyword and keyword not in haystack:
                continue
            rows.append(source)
        sort_mode = self.sort_combo.currentData() if hasattr(self, "sort_combo") else "attempt_desc"
        if sort_mode == "order_asc":
            rows.sort(key=lambda row: (row["sort_order"], row["id"]))
        elif sort_mode == "name_asc":
            rows.sort(key=lambda row: (row["display_name"].lower(), row["id"]))
        else:
            rows.sort(key=lambda row: ((row["last_attempt_at"] or ""), row["sort_order"], row["id"]), reverse=True)
        self._rows = rows
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for index, source in enumerate(rows):
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(index, 0, check)
            result = {}
            try:
                result = json.loads(source["last_result_json"] or "{}")
            except json.JSONDecodeError:
                result = {}
            summary = ""
            if result:
                summary = f"新增{result.get('saved_new', 0)} 更新{result.get('body_updated', 0)} 失败{result.get('failed', 0)}"
            status = _STATUS_LABELS.get(source["last_status"] or "", source["last_status"] or "尚未检查")
            if summary:
                status = f"{status}（{summary}）"
            values = [
                source["display_name"],
                _MODE_LABELS.get(source["collect_mode"], source["collect_mode"]),
                str(catalog.count_source_contents(self.database, source)),
                source["last_attempt_at"] or "尚未检查",
                source["last_success_at"] or "尚无成功完成",
                status,
            ]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(source["canonical_url"])
                self.table.setItem(index, column, item)
        self.table.blockSignals(False)
        self.empty_label.setVisible(not rows)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            return

    def _checked_rows(self):
        chosen = []
        for index, source in enumerate(self._rows):
            item = self.table.item(index, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                chosen.append(source)
        return chosen

    def _selected(self):
        indexes = self.table.selectionModel().selectedRows()
        if indexes:
            return self._rows[indexes[0].row()]
        chosen = self._checked_rows()
        return chosen[0] if chosen else None

    def _add(self) -> None:
        text, ok = QInputDialog.getText(self, "添加常用来源", "粘贴一个知乎链接：")
        if not ok or not str(text).strip():
            return
        parsed = parse_zhihu_url(str(text).strip())
        identity, mode, error = identity_for(parsed, "auto")
        if error or not identity:
            QMessageBox.warning(self, "添加常用来源", error or "无法识别这个链接。")
            return
        preset = self.preset_manager.get_preset(self.preset_manager.last_selected_id)
        snapshot = preset.to_dict() if preset is not None else {}
        catalog.save_source(
            self.database,
            display_name=parsed.canonical,
            canonical_url=parsed.canonical,
            target_kind=parsed.kind,
            identity_key=identity,
            collect_mode=mode,
            preset_id=preset.id if preset is not None else "",
            preset_snapshot=snapshot,
        )
        self.reload()

    def _import_history(self) -> None:
        candidates = history_source_candidates(self.database)
        if not candidates:
            QMessageBox.information(self, "从历史导入", "历史里还没有可识别的作者、问题、回答或收藏夹。")
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("从历史导入")
        dialog.setText("勾选要变成常用来源的历史目标。不会自动把全部历史都加进来。")
        box = QVBoxLayout()
        checks = []
        holder = QWidget()
        for candidate in candidates:
            count = catalog.count_source_contents(self.database, {
                "target_kind": candidate["target_kind"],
                "collect_mode": candidate["collect_mode"],
                "identity_key": candidate["identity_key"],
                "canonical_url": candidate["canonical_url"],
            })
            label = (
                f"{candidate['canonical_url']}  {_MODE_LABELS.get(candidate['collect_mode'], candidate['collect_mode'])}"
                f"  本地已有 {count} 篇"
            )
            check = QCheckBox(label)
            checks.append((check, candidate))
            box.addWidget(check)
        holder.setLayout(box)
        dialog.layout().addWidget(holder)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if dialog.exec() != QMessageBox.StandardButton.Ok:
            return
        preset = self.preset_manager.get_preset(self.preset_manager.last_selected_id)
        for check, candidate in checks:
            if not check.isChecked():
                continue
            try:
                options = json.loads(candidate["options_json"] or "{}")
            except json.JSONDecodeError:
                options = {}
            snapshot = preset.to_dict() if preset is not None else {}
            snapshot.update({key: value for key, value in options.items() if key in snapshot or key == "execution_intent"})
            catalog.save_source(
                self.database,
                display_name=candidate["canonical_url"],
                canonical_url=candidate["canonical_url"],
                target_kind=candidate["target_kind"],
                identity_key=candidate["identity_key"],
                collect_mode=candidate["collect_mode"],
                preset_id=str(options.get("preset_id") or (preset.id if preset is not None else "")),
                preset_snapshot=snapshot,
            )
        self.reload()

    def _rename(self) -> None:
        source = self._selected()
        if source is None:
            QMessageBox.information(self, "常用来源", "请先选择一个来源。")
            return
        name, ok = QInputDialog.getText(self, "修改名称", "显示名称：", text=source["display_name"])
        if not ok or not str(name).strip():
            return
        catalog.update_source(self.database, int(source["id"]), display_name=str(name).strip())
        self.reload()

    def _edit_options(self) -> None:
        source = self._selected()
        if source is None:
            QMessageBox.information(self, "常用来源", "请先选择一个来源。")
            return
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout
        import sqlite3

        dlg = QDialog(self)
        dlg.setWindowTitle(f"编辑默认选项 - {source['display_name']}")
        dlg.resize(420, 200)
        form = QFormLayout(dlg)
        name_edit = QLineEdit(source["display_name"])
        mode_combo = QComboBox()

        target_kind = str(source["target_kind"] or "") if "target_kind" in source.keys() else ""
        allowed_modes = {
            "user": [("answers", "答主回答"), ("pins", "答主想法"), ("all", "答主全部内容")],
            "question": [("question", "问题全部回答")],
            "answer": [("single_answer", "仅此回答")],
            "collection": [("collection", "收藏夹回答")],
        }.get(target_kind)
        if not allowed_modes:
            allowed_modes = [
                ("answers", "答主回答"),
                ("pins", "答主想法"),
                ("all", "答主全部内容"),
                ("question", "问题全部回答"),
                ("single_answer", "仅此回答"),
                ("collection", "收藏夹回答"),
            ]
        mode_keys = {m[0] for m in allowed_modes}
        if source["collect_mode"] not in mode_keys:
            allowed_modes.append((source["collect_mode"], _MODE_LABELS.get(source["collect_mode"], source["collect_mode"])))

        for mode, label in allowed_modes:
            mode_combo.addItem(label, mode)
        cur_mode_idx = mode_combo.findData(source["collect_mode"])
        if cur_mode_idx >= 0:
            mode_combo.setCurrentIndex(cur_mode_idx)
        if len(allowed_modes) == 1:
            mode_combo.setEnabled(False)

        preset_combo = QComboBox()
        preset_combo.addItem("跟随默认配置 / 独立快照", "")
        for p in self.preset_manager.presets:
            preset_combo.addItem(p.name, p.id)
        cur_preset_idx = preset_combo.findData(source["preset_id"] or "")
        if cur_preset_idx >= 0:
            preset_combo.setCurrentIndex(cur_preset_idx)

        form.addRow("显示名称", name_edit)
        form.addRow("采集范围", mode_combo)
        form.addRow("默认配置预设", preset_combo)

        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bbox.accepted.connect(dlg.accept)
        bbox.rejected.connect(dlg.reject)
        form.addRow(bbox)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_name = name_edit.text().strip() or source["canonical_url"]
        new_mode = mode_combo.currentData()
        new_preset_id = preset_combo.currentData() or ""
        new_preset = self.preset_manager.get_preset(new_preset_id) if new_preset_id else None
        snapshot = new_preset.to_dict() if new_preset else None

        try:
            catalog.update_source(
                self.database,
                int(source["id"]),
                display_name=new_name,
                collect_mode=new_mode,
                preset_id=new_preset_id,
                preset_snapshot=snapshot,
            )
        except sqlite3.IntegrityError:
            QMessageBox.warning(
                self,
                "常用来源",
                f"已存在相同目标与采集模式的常用来源（{_MODE_LABELS.get(new_mode, new_mode)}），无法保存重复项。",
            )
            return
        self.reload()

    def _move(self, direction: int) -> None:
        source = self._selected()
        if source is None:
            QMessageBox.information(self, "常用来源", "请先选择一个来源。")
            return
        if hasattr(self, "sort_combo") and self.sort_combo.currentData() != "order_asc":
            self.sort_combo.setCurrentIndex(self.sort_combo.findData("order_asc"))
        catalog.move_source_order(self.database, int(source["id"]), direction)
        self.reload()

    def _remove(self) -> None:
        chosen = self._checked_rows() or ([self._selected()] if self._selected() is not None else [])
        if not chosen:
            QMessageBox.information(self, "常用来源", "请先勾选要移除的来源。")
            return
        answer = QMessageBox.question(self, "移除来源", "只移除快捷入口，正文、图片、导出文件和历史任务都会保留。")
        if answer != QMessageBox.StandardButton.Yes:
            return
        for source in chosen:
            catalog.delete_source(self.database, int(source["id"]))
        self.reload()

    def _open_web(self) -> None:
        source = self._selected()
        if source is None:
            return
        QDesktopServices.openUrl(QUrl(source["canonical_url"]))

    def _open_library(self) -> None:
        source = self._selected()
        if source is None:
            QMessageBox.information(self, "常用来源", "请先选择一个来源。")
            return
        self.on_open_library(int(source["id"]))

    def _confirm_batch_export(self, chosen: list, requests: list) -> bool:
        from pathlib import Path
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QLabel, QTableWidget,
            QTableWidgetItem, QRadioButton, QButtonGroup, QDialogButtonBox
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("确认更新并导出 - 常用来源")
        dlg.resize(680, 480)
        layout = QVBoxLayout(dlg)

        has_overwrite = any(getattr(r, "conflict_policy", "") == "overwrite" for r in requests)

        info = QLabel(f"<b>共选择 {len(requests)} 个常用来源进行更新并导出：</b>")
        layout.addWidget(info)

        table = QTableWidget(len(requests), 5)
        table.setHorizontalHeaderLabels(["来源名称", "采集范围", "格式", "输出目录", "预设冲突策略"])
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        default_output = ""
        parent = self.parent()
        if parent is not None and hasattr(parent, "settings"):
            default_output = str(parent.settings.resolved_output_dir())
        if not default_output:
            default_output = str(Path("output").resolve())

        for idx, (source, req) in enumerate(zip(chosen, requests)):
            formats = []
            if req.export_markdown:
                formats.append("MD")
            if req.export_docx:
                formats.append("Docx")
            if req.export_pdf:
                formats.append("PDF")
            policy_label = {
                "new_copy": "新建副本",
                "overwrite": "覆盖现有⚠️",
                "skip": "跳过已有",
            }.get(req.conflict_policy, req.conflict_policy or "新建副本")

            out_dir = req.output_dir or default_output

            src_name = str(source["display_name"] or "") if "display_name" in source.keys() else ""
            src_mode = str(source["collect_mode"] or "") if "collect_mode" in source.keys() else ""
            row_items = [
                QTableWidgetItem(src_name),
                QTableWidgetItem(_MODE_LABELS.get(src_mode, src_mode)),
                QTableWidgetItem("+".join(formats) or "无"),
                QTableWidgetItem(str(out_dir)),
                QTableWidgetItem(policy_label),
            ]
            for col, item in enumerate(row_items):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(idx, col, item)

        layout.addWidget(table)

        if has_overwrite:
            warn = QLabel("⚠️ <b>注意：部分来源的预设配置了「覆盖现有文件」！</b>更新时若目标已存在同名文件将被直接覆盖替换。")
            warn.setStyleSheet("color: #b91c1c; font-weight: bold;")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        group_box = QWidget()
        group_layout = QVBoxLayout(group_box)
        group_layout.setContentsMargins(0, 4, 0, 4)
        group_layout.addWidget(QLabel("选择本次执行的冲突处理策略："))

        bgroup = QButtonGroup(dlg)
        rb_keep = QRadioButton("按各来源预设执行（如上表所示）")
        rb_new_copy = QRadioButton("统一设为：生成新副本（推荐，保护已有文件）")
        rb_skip = QRadioButton("统一设为：跳过已有文件")
        rb_overwrite = QRadioButton("统一设为：覆盖同名文件（谨慎选择）")

        bgroup.addButton(rb_keep)
        bgroup.addButton(rb_new_copy)
        bgroup.addButton(rb_skip)
        bgroup.addButton(rb_overwrite)

        rb_keep.setChecked(True)

        group_layout.addWidget(rb_keep)
        group_layout.addWidget(rb_new_copy)
        group_layout.addWidget(rb_skip)
        group_layout.addWidget(rb_overwrite)
        layout.addWidget(group_box)

        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bbox.button(QDialogButtonBox.StandardButton.Ok).setText("确认并加入队列")
        bbox.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bbox.accepted.connect(dlg.accept)
        bbox.rejected.connect(dlg.reject)
        layout.addWidget(bbox)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False

        if rb_new_copy.isChecked():
            for req in requests:
                req.conflict_policy = "new_copy"
        elif rb_skip.isChecked():
            for req in requests:
                req.conflict_policy = "skip"
        elif rb_overwrite.isChecked():
            for req in requests:
                req.conflict_policy = "overwrite"

        return True

    def _enqueue(self, intent: str) -> None:
        chosen = self._checked_rows()
        if not chosen:
            QMessageBox.information(self, "常用来源", "请先勾选要更新的来源。")
            return
        requests = []
        source_ids = []
        for source in chosen:
            request = self._request_for_source(source, intent)
            if request is None:
                return
            requests.append(request)
            source_ids.append(int(source["id"]))

        if intent == "library":
            names = [s["display_name"] for s in chosen]
            names_summary = "、".join(names[:3]) + (f" 等共 {len(names)} 个来源" if len(names) > 3 else "")
            answer = QMessageBox.question(
                self,
                "更新内容库",
                f"将仅更新内容库（保存至本地数据库，不生成导出文件）：\n{names_summary}\n\n是否确认加入队列并开始？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        else:
            from pathlib import Path
            from zhihu_collector.export_plan import ExportPlan
            from zhihu_collector.gui.confirm_plan_dialog import ConfirmPlanDialog
            from PySide6.QtWidgets import QDialog

            default_output = ""
            parent = self.parent()
            if parent is not None and hasattr(parent, "settings"):
                default_output = str(parent.settings.resolved_output_dir())
            if not default_output:
                default_output = str(Path("output").resolve())

            if len(requests) == 1:
                req = requests[0]
                plan = ExportPlan(
                    scope_kind="network_plan",
                    source_title=chosen[0]["display_name"],
                    source_url=chosen[0]["canonical_url"],
                    source_id=chosen[0]["identity_key"] if "identity_key" in chosen[0].keys() else "",
                    export_markdown=req.export_markdown,
                    export_docx=req.export_docx,
                    export_pdf=req.export_pdf,
                    export_separate=req.export_separate,
                    export_merged=req.export_merged,
                    download_images=req.download_images,
                    save_metadata=req.save_metadata,
                    output_root=req.output_dir or default_output,
                    conflict_policy=req.conflict_policy or "new_copy",
                    max_items=req.max_items,
                )
                confirm_dlg = ConfirmPlanDialog(plan, self)
                if confirm_dlg.exec() != QDialog.DialogCode.Accepted or not confirm_dlg.confirmed:
                    return
                req.conflict_policy = plan.conflict_policy
            else:
                if not self._confirm_batch_export(chosen, requests):
                    return

        self.on_enqueue(requests, source_ids)

    def _request_for_source(self, source, intent: str):
        from zhihu_collector.collectors.service import collect_request_from_snapshot

        preset = self.preset_manager.get_preset(source["preset_id"] or "")
        if preset is not None:
            data = preset.to_dict()
        else:
            try:
                data = json.loads(source["preset_snapshot_json"] or "{}")
            except json.JSONDecodeError:
                data = {}
            if source["preset_id"]:
                QMessageBox.information(
                    self,
                    "常用来源",
                    f"「{source['display_name']}」关联的配置已经不在了，这次使用保存来源时复制下来的默认值。",
                )
        data["url"] = source["canonical_url"]
        data["mode"] = source["collect_mode"]
        data["execution_intent"] = intent
        data["source_id"] = int(source["id"])
        data.pop("id", None)
        data.pop("name", None)
        data.pop("remember_output_dir", None)
        if not data.get("output_dir"):
            data["output_dir"] = ""
        return collect_request_from_snapshot(data)
