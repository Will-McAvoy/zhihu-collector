"""浏览和离线导出已经保存在数据库里的内容，包含增强的文章阅读预览与多页勾选。"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.offline import export_local_contents, offline_plan_counts
from zhihu_collector.collectors.service import CollectRequest
from zhihu_collector.config import Settings, data_dir
from zhihu_collector.database import catalog
from zhihu_collector.export_plan import ExportPlan
from zhihu_collector.gui.confirm_plan_dialog import ConfirmPlanDialog
from zhihu_collector.gui.preview_renderer import registered_image_roots, render_article_preview

_PAGE_SIZE = 20
_TYPE_LABELS = {"answer": "回答", "pin": "想法"}


def export_status_text(success: int, failed: int, stale: bool = False) -> str:
    if stale:
        return "正文已更新，导出待更新"
    if failed and success:
        return "部分导出"
    if failed:
        return "导出失败"
    if success:
        return "已导出"
    return "未导出"


def _safe_external_url(value: str | QUrl) -> str:
    """只允许带主机的 http/https 链接进入外部浏览器。"""
    url = value if isinstance(value, QUrl) else QUrl(str(value or "").strip())
    if url.scheme().lower() not in {"http", "https"} or not url.host():
        return ""
    return url.toString()


class LibraryWindow(QDialog):
    def __init__(self, database, settings: Settings, parent=None, source_id: int | None = None, task_id: int | None = None):
        super().__init__(parent)
        self.database = database
        self.settings = settings
        self.offset = 0
        self.total = 0
        self._rows = []
        self._selected_ids: set[int] = set()
        self._selection_order: list[int] = []
        self._current_preview_id: int | None = None
        self._font_size = 15
        self._fixed_source_id = source_id
        self._task_id = task_id
        self._task_outcome = ""

        self.setWindowTitle("本地内容库与文章阅读")
        self.resize(1180, 720)
        self.setFont(QFont("Microsoft YaHei", 9))
        self._build()
        self._reload_collections()
        self.reload()

    def _build(self) -> None:
        main_layout = QVBoxLayout(self)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter = self.splitter
        main_layout.addWidget(splitter)

        # ====== 左侧：筛选、列表与翻页 ======
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # 筛选控件
        filters = QHBoxLayout()
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("搜索标题或正文")
        self.author = QLineEdit()
        self.author.setPlaceholderText("作者")
        self.kind = QComboBox()
        self.kind.addItem("全部类型", "")
        self.kind.addItem("回答", "answer")
        self.kind.addItem("想法", "pin")

        self.collection_combo = QComboBox()
        self.collection_combo.addItem("全部收藏夹 / 独立内容", "")

        search_btn = QPushButton("搜索")
        search_btn.clicked.connect(self.search_first)
        self.recent_task_btn = QPushButton("查看最近任务")
        self.recent_task_btn.clicked.connect(self._view_recent_task)

        filters.addWidget(self.keyword, 2)
        filters.addWidget(self.author, 1)
        filters.addWidget(self.kind, 1)
        filters.addWidget(self.collection_combo, 2)
        filters.addWidget(search_btn)
        filters.addWidget(self.recent_task_btn)
        left_layout.addLayout(filters)

        extra = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.addItem("全部常用来源", 0)
        self.read_combo = QComboBox()
        self.read_combo.addItem("全部阅读状态", "")
        self.read_combo.addItem("未读", "unread")
        self.read_combo.addItem("已读", "read")
        self.star_only = QCheckBox("仅星标")
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("标签")
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("按入库顺序", "id_desc")
        self.sort_combo.addItem("发布时间从新到旧", "published_desc")
        self.sort_combo.addItem("发布时间从旧到新", "published_asc")
        self.sort_combo.addItem("最近检查从新到旧", "checked_desc")
        self.sort_combo.addItem("最近检查从旧到新", "checked_asc")
        self.sort_combo.addItem("赞同数从高到低", "votes_desc")
        self.sort_combo.addItem("赞同数从低到高", "votes_asc")
        self.date_field = QComboBox()
        self.date_field.addItem("发布时间", "published")
        self.date_field.addItem("采集时间", "collected")
        self.date_from = QLineEdit()
        self.date_from.setPlaceholderText("开始 YYYY-MM-DD")
        self.date_to = QLineEdit()
        self.date_to.setPlaceholderText("结束 YYYY-MM-DD")
        for widget in (
            self.source_combo, self.read_combo, self.star_only, self.tag_edit, self.sort_combo,
            self.date_field, self.date_from, self.date_to,
        ):
            extra.addWidget(widget)
        left_layout.addLayout(extra)

        self.task_filter_bar = QFrame()
        self.task_filter_bar.setStyleSheet("background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 2px;")
        tf_layout = QHBoxLayout(self.task_filter_bar)
        tf_layout.setContentsMargins(6, 2, 6, 2)
        self.task_filter_label = QLabel("🔍 任务结果筛选")
        self.task_filter_label.setStyleSheet("color: #1e40af; font-weight: 500;")
        self.task_outcome_combo = QComboBox()
        self.task_outcome_combo.addItem("全部本次结果", "")
        self.task_outcome_combo.addItem("仅本次新增", "inserted")
        self.task_outcome_combo.addItem("仅本次更新", "updated")
        self.task_outcome_combo.currentIndexChanged.connect(self._on_task_outcome_changed)
        self.clear_task_filter_btn = QPushButton("清除任务筛选")
        self.clear_task_filter_btn.clicked.connect(self._clear_task_filter)
        tf_layout.addWidget(self.task_filter_label)
        tf_layout.addWidget(self.task_outcome_combo)
        tf_layout.addWidget(self.clear_task_filter_btn)
        tf_layout.addStretch()
        self.task_filter_bar.setVisible(bool(self._task_id))
        left_layout.addWidget(self.task_filter_bar)

        # 勾选操作栏
        selection_bar = QHBoxLayout()
        self.select_page_btn = QPushButton("选择本页")
        self.clear_sel_btn = QPushButton("清空勾选")
        select_page_btn = self.select_page_btn
        clear_sel_btn = self.clear_sel_btn
        select_page_btn.clicked.connect(self._select_current_page)
        clear_sel_btn.clicked.connect(self._clear_selection)
        self.selection_label = QLabel("已勾选：0 篇 (跨页)")
        self.selection_label.setStyleSheet("font-weight: bold; color: #1a73e8;")
        self.tag_add_btn = QPushButton("加标签")
        self.tag_remove_btn = QPushButton("移除标签")
        self.star_btn = QPushButton("加星")
        self.unstar_btn = QPushButton("取消星标")
        self.read_btn = QPushButton("标为已读")
        self.unread_btn = QPushButton("标为未读")
        self.tag_add_btn.clicked.connect(self._add_tags)
        self.tag_remove_btn.clicked.connect(self._remove_tags)
        self.star_btn.clicked.connect(lambda: self._set_star(True))
        self.unstar_btn.clicked.connect(lambda: self._set_star(False))
        self.read_btn.clicked.connect(lambda: self._set_read(True))
        self.unread_btn.clicked.connect(lambda: self._set_read(False))

        selection_bar.addWidget(select_page_btn)
        selection_bar.addWidget(clear_sel_btn)
        selection_bar.addWidget(self.selection_label)
        for button in (self.tag_add_btn, self.tag_remove_btn, self.star_btn, self.unstar_btn, self.read_btn, self.unread_btn):
            selection_bar.addWidget(button)
        selection_bar.addStretch()
        left_layout.addLayout(selection_bar)

        # 表格
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(["选择", "标题", "作者", "类型", "更新时间", "导出状态", "阅读", "星标", "标签"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 45)
        self.table.setColumnWidth(1, 240)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 60)
        self.table.setColumnWidth(4, 110)
        self.table.itemSelectionChanged.connect(self._show_preview)
        self.table.itemChanged.connect(self._on_item_changed)
        left_layout.addWidget(self.table)

        # 翻页与主操作按钮
        page_row = QHBoxLayout()
        self.page_label = QLabel("第 0 条")
        previous_page = QPushButton("上一页")
        next_page = QPushButton("下一页")
        previous_page.clicked.connect(self._previous)
        next_page.clicked.connect(self._next)

        page_row.addWidget(previous_page)
        page_row.addWidget(next_page)
        page_row.addWidget(self.page_label)
        page_row.addStretch()
        left_layout.addLayout(page_row)

        action_row = QHBoxLayout()
        open_url = QPushButton("打开原文")
        open_file = QPushButton("打开导出文件")
        self.reexport_btn = QPushButton("重新导出已选内容")
        self.reexport_btn.setStyleSheet("background-color: #0066cc; color: white; font-weight: bold; padding: 4px 10px;")

        open_url.clicked.connect(self._open_original)
        open_file.clicked.connect(self._open_export)
        self.reexport_btn.clicked.connect(self._reexport)

        action_row.addWidget(open_url)
        action_row.addWidget(open_file)
        action_row.addWidget(self.reexport_btn)
        left_layout.addLayout(action_row)

        splitter.addWidget(left_widget)

        # ====== 右侧：增强文章阅读预览 ======
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 0, 0, 0)

        # 文章头部信息区
        header_frame = QFrame()
        header_frame.setFrameShape(QFrame.Shape.StyledPanel)
        header_layout = QVBoxLayout(header_frame)
        self.preview_title = QLabel("请在左侧列表选择一篇文章进行阅读")
        self.preview_title.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        self.preview_title.setWordWrap(True)
        self.mark_read_btn = QPushButton("标为已读")
        self.mark_read_btn.clicked.connect(lambda: self._set_read(True, current_only=True))
        header_layout.addWidget(self.preview_title)
        header_layout.addWidget(self.mark_read_btn)

        self.preview_meta = QLabel("")
        self.preview_meta.setStyleSheet("color: #666; font-size: 12px;")
        self.preview_meta.setWordWrap(True)
        self.preview_meta.setOpenExternalLinks(False)
        self.preview_meta.linkActivated.connect(lambda link: self._handle_anchor_clicked(QUrl(link)))
        header_layout.addWidget(self.preview_meta)
        right_layout.addWidget(header_frame)

        # 阅读控制栏：缩放与页内查找
        control_bar = QHBoxLayout()
        zoom_in_btn = QPushButton("放大 A+")
        zoom_out_btn = QPushButton("缩小 A-")
        zoom_reset_btn = QPushButton("重置 100%")
        zoom_in_btn.clicked.connect(self._zoom_in)
        zoom_out_btn.clicked.connect(self._zoom_out)
        zoom_reset_btn.clicked.connect(self._zoom_reset)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("页内查找...")
        find_btn = QPushButton("查找下一个")
        find_btn.clicked.connect(self._find_in_page)
        self.search_input.returnPressed.connect(self._find_in_page)

        control_bar.addWidget(zoom_in_btn)
        control_bar.addWidget(zoom_out_btn)
        control_bar.addWidget(zoom_reset_btn)
        control_bar.addSpacing(16)
        control_bar.addWidget(self.search_input)
        control_bar.addWidget(find_btn)
        right_layout.addLayout(control_bar)

        # 富文本阅读器
        self.preview_browser = QTextBrowser()
        self.preview = self.preview_browser
        self.preview_browser.setOpenExternalLinks(False)
        self.preview_browser.anchorClicked.connect(self._handle_anchor_clicked)
        self.preview_browser.setPlaceholderText("选择左侧内容后，这里将显示排版完整、带本地配图的离线正文。不会执行抓取页面中的任何脚本。")
        right_layout.addWidget(self.preview_browser)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 6)

    def _reload_collections(self) -> None:
        self.collection_combo.clear()
        self.collection_combo.addItem("全部收藏夹 / 独立内容", "")
        collections = self.database.list_collections()
        for coll in collections:
            title = coll["title"] or f"收藏夹 {coll['zhihu_collection_id']}"
            self.collection_combo.addItem(f"📁 {title}", coll["zhihu_collection_id"])
        current_source = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("全部常用来源", 0)
        for source in catalog.list_sources(self.database):
            label = f"{source['display_name']}（{source['collect_mode']}）"
            self.source_combo.addItem(label, int(source["id"]))
        target = self._fixed_source_id or current_source or 0
        index = self.source_combo.findData(target)
        if index >= 0:
            self.source_combo.setCurrentIndex(index)
        self.source_combo.blockSignals(False)

    def search_first(self) -> None:
        self.offset = 0
        self.reload()

    def _on_task_outcome_changed(self) -> None:
        self._task_outcome = self.task_outcome_combo.currentData() or ""
        self.search_first()

    def _clear_task_filter(self) -> None:
        self._task_id = None
        self._task_outcome = ""
        self.task_filter_bar.setVisible(False)
        self.search_first()

    def _view_recent_task(self) -> None:
        tasks = self.database.list_tasks()
        if not tasks:
            QMessageBox.information(self, "最近任务", "本地尚无任务记录。")
            return
        latest = tasks[0]
        self._task_id = int(latest["id"])
        self._task_outcome = ""
        self._fixed_source_id = None
        self.task_filter_label.setText(f"🔍 当前筛选：任务 #{self._task_id} ({latest['target_url'] or latest['task_type']})")
        self.task_outcome_combo.blockSignals(True)
        self.task_outcome_combo.setCurrentIndex(0)
        self.task_outcome_combo.blockSignals(False)
        self.task_filter_bar.setVisible(True)
        self.search_first()

    def _search_kwargs(self) -> dict:
        published_from = published_to = collected_from = collected_to = ""
        start = self.date_from.text().strip()
        end = self.date_to.text().strip()
        if self.date_field.currentData() == "collected":
            collected_from, collected_to = start, end
        else:
            published_from, published_to = start, end
        source_id = self._fixed_source_id or int(self.source_combo.currentData() or 0)
        return {
            "keyword": self.keyword.text(),
            "author": self.author.text(),
            "content_type": self.kind.currentData() or "",
            "collection_id": self.collection_combo.currentData() or "",
            "source_id": source_id or None,
            "read_state": self.read_combo.currentData() or "",
            "starred_only": self.star_only.isChecked(),
            "tag": self.tag_edit.text().strip(),
            "published_from": published_from,
            "published_to": published_to,
            "collected_from": collected_from,
            "collected_to": collected_to,
            "sort": self.sort_combo.currentData() or "id_desc",
            "task_id": self._task_id,
            "task_outcome": self._task_outcome,
            "offset": self.offset,
            "limit": _PAGE_SIZE,
        }

    def reload(self) -> None:
        if self._task_id:
            self.task_filter_bar.setVisible(True)
            self.task_filter_label.setText(f"🔍 当前筛选：任务 #{self._task_id}")
        else:
            self.task_filter_bar.setVisible(False)
        rows, total = self.database.search_contents(**self._search_kwargs())
        self._rows = rows
        self.total = total

        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            cid = int(row["id"])
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if cid in self._selected_ids else Qt.CheckState.Unchecked)
            self.table.setItem(index, 0, check)

            values = [
                row["title"] or "未命名",
                row["author_name"] or "",
                _TYPE_LABELS.get(row["content_type"], row["content_type"] or ""),
                row["updated_time"] or row["created_time"] or "",
                export_status_text(
                    int(row["export_success"] or 0),
                    int(row["export_failed"] or 0),
                    stale=bool(row["export_stale"]),
                ),
                "已读" if int(row["is_read"] or 0) else "未读",
                "已加星" if int(row["starred"] or 0) else "",
                row["tag_names"] or "",
            ]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(str(value))
                self.table.setItem(index, column, item)

        self.table.blockSignals(False)

        start = 0 if total == 0 else self.offset + 1
        end = min(total, self.offset + len(rows))
        self.page_label.setText(f"{start}-{end} / {total}")
        self._update_selection_ui()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            row_idx = item.row()
            if 0 <= row_idx < len(self._rows):
                cid = int(self._rows[row_idx]["id"])
                if item.checkState() == Qt.CheckState.Checked:
                    self._selected_ids.add(cid)
                    if cid not in self._selection_order:
                        self._selection_order.append(cid)
                else:
                    self._selected_ids.discard(cid)
                    if cid in self._selection_order:
                        self._selection_order.remove(cid)
                self._update_selection_ui()

    def _select_current_page(self) -> None:
        self.table.blockSignals(True)
        for index, row in enumerate(self._rows):
            cid = int(row["id"])
            self._selected_ids.add(cid)
            if cid not in self._selection_order:
                self._selection_order.append(cid)
            item = self.table.item(index, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False)
        self._update_selection_ui()

    def _clear_selection(self) -> None:
        self._selected_ids.clear()
        self._selection_order.clear()
        self.table.blockSignals(True)
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_selection_ui()

    def _update_selection_ui(self) -> None:
        count = len(self._selected_ids)
        self.selection_label.setText(f"已勾选：{count} 篇 (跨页，不是仅当前页)")
        self.reexport_btn.setText(f"重新导出已选内容 ({count} 篇)" if count else "重新导出")

    def _chosen_ids(self, *, current_only: bool = False) -> list[int]:
        if current_only and self._current_preview_id:
            return [int(self._current_preview_id)]
        return [cid for cid in self._selection_order if cid in self._selected_ids]

    def _add_tags(self) -> None:
        ids = self._chosen_ids()
        if not ids:
            QMessageBox.information(self, "标签", "请先勾选内容。当前操作作用于已勾选的全部篇目。")
            return
        text, ok = QInputDialog.getText(self, "添加标签", f"给已勾选的 {len(ids)} 篇添加标签，多个标签用空格或逗号分隔：")
        if not ok:
            return
        names = [part for part in str(text).replace("，", " ").replace(",", " ").split(" ") if part.strip()]
        saved = catalog.add_tags(self.database, ids, names)
        if not saved:
            QMessageBox.information(self, "标签", "没有可保存的标签。空标签不会写入。")
            return
        self.reload()

    def _remove_tags(self) -> None:
        ids = self._chosen_ids()
        if not ids:
            QMessageBox.information(self, "标签", "请先勾选内容。")
            return
        text, ok = QInputDialog.getText(self, "移除标签", f"从已勾选的 {len(ids)} 篇移除标签：")
        if not ok:
            return
        names = [part for part in str(text).replace("，", " ").replace(",", " ").split(" ") if part.strip()]
        catalog.remove_tags(self.database, ids, names)
        self.reload()

    def _set_star(self, starred: bool) -> None:
        ids = self._chosen_ids()
        if not ids:
            QMessageBox.information(self, "星标", "请先勾选内容。")
            return
        catalog.set_starred(self.database, ids, starred=starred)
        self.reload()

    def _set_read(self, is_read: bool, *, current_only: bool = False) -> None:
        ids = self._chosen_ids(current_only=current_only)
        if not ids:
            QMessageBox.information(self, "阅读状态", "请先勾选内容，或打开一篇后再标记。")
            return
        catalog.set_read_state(self.database, ids, is_read=is_read)
        self.reload()

    def _previous(self) -> None:
        self.offset = max(0, self.offset - _PAGE_SIZE)
        self.reload()

    def _next(self) -> None:
        if self.offset + _PAGE_SIZE < self.total:
            self.offset += _PAGE_SIZE
            self.reload()

    def _current_row(self):
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            QMessageBox.information(self, "本地内容库", "请先选择一篇内容。")
            return None
        return self._rows[indexes[0].row()]

    def _show_preview(self) -> None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            self._clear_preview()
            return
        row_data = self._rows[indexes[0].row()]
        content_id = int(row_data["id"])
        self._current_preview_id = content_id

        full_row = self.database.get_content(content_id)
        if full_row is None:
            self._clear_preview()
            return

        images = [dict(img) for img in self.database.list_images(content_id)]

        title_text = full_row["title"] or "未命名内容"
        author_text = full_row["author_name"] or "未知作者"
        time_text = full_row["updated_time"] or full_row["created_time"] or "未知时间"
        url = full_row["url"] or ""
        safe_url = _safe_external_url(url)

        self.preview_title.setText(title_text)
        link_html = f'<a href="{safe_url}">[打开知乎原文]</a>' if safe_url else ""
        self.preview_meta.setText(f"作者：{author_text}   |   发布/更新：{time_text}   {link_html}")

        # 异步/竞态防护：核对当前内容 ID
        if self._current_preview_id == content_id:
            allowed_roots = [
                self.settings.resolved_output_dir(),
                data_dir(),
                *registered_image_roots([img.get("local_path") or "" for img in images]),
            ]
            html = render_article_preview(
                full_row,
                images,
                base_font_size=self._font_size,
                allowed_roots=allowed_roots,
            )
            self.preview_browser.setHtml(html)

    def _clear_preview(self) -> None:
        self._current_preview_id = None
        self.preview_title.setText("请在左侧列表选择一篇文章进行阅读")
        self.preview_meta.setText("")
        self.preview_browser.clear()

    def _zoom_in(self) -> None:
        self._font_size = min(28, self._font_size + 2)
        self._refresh_preview_font()

    def _zoom_out(self) -> None:
        self._font_size = max(11, self._font_size - 2)
        self._refresh_preview_font()

    def _zoom_reset(self) -> None:
        self._font_size = 15
        self._refresh_preview_font()

    def _refresh_preview_font(self) -> None:
        if self._current_preview_id is not None:
            self._show_preview()

    def _find_in_page(self) -> None:
        text = self.search_input.text().strip()
        if text:
            found = self.preview_browser.find(text)
            if not found:
                # 重新从开头查找
                cursor = self.preview_browser.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                self.preview_browser.setTextCursor(cursor)
                self.preview_browser.find(text)

    def _handle_anchor_clicked(self, qurl: QUrl | str) -> None:
        target = qurl if isinstance(qurl, QUrl) else QUrl(str(qurl))
        safe_target = _safe_external_url(target)
        if safe_target:
            QDesktopServices.openUrl(QUrl(safe_target))
        else:
            logger.warning("预览拦截非安全协议链接：%s", target.toString())

    def _open_original(self) -> None:
        row = self._current_row()
        if row is None:
            return
        safe_url = _safe_external_url(row["url"] or "")
        if not safe_url:
            QMessageBox.warning(self, "打开原文", "这条内容的原文链接不是安全的 http/https 地址。")
            return
        QDesktopServices.openUrl(QUrl(safe_url))

    def _open_export(self) -> None:
        row = self._current_row()
        if row is None:
            return
        paths = [path for path in self.database.successful_export_paths(int(row["id"])) if Path(path).is_file()]
        if not paths:
            QMessageBox.information(self, "本地内容库", "这篇内容还没有可打开的导出文件。")
            return
        os.startfile(paths[0])

    def _reexport(self) -> None:
        chosen_ids = [cid for cid in self._selection_order if cid in self._selected_ids]
        if not chosen_ids:
            # 如果没有跨页勾选，尝试取当前选中行
            current = self._current_row()
            if current is not None:
                chosen_ids = [int(current["id"])]
            else:
                QMessageBox.information(self, "重新导出", "请先在左侧表格勾选或选中要导出的内容。")
                return

        # 整理要导出的文章详细清单
        member_items = []
        for cid in chosen_ids:
            r = self.database.get_content(cid)
            if r is not None:
                member_items.append(
                    {
                        "id": str(cid),
                        "title": r["title"] or "未命名",
                        "author": r["author_name"] or "未知作者",
                        "status": "本地已保存",
                    }
                )

        # 构造精确导出计划并弹出确认对话框
        plan = ExportPlan(
            scope_kind="offline_exact",
            source_title="本地内容库勾选导出",
            source_id=f"{len(chosen_ids)} 篇",
            member_ids=[str(cid) for cid in chosen_ids],
            member_items=member_items,
            export_markdown=self.settings.export_markdown,
            export_docx=self.settings.export_docx,
            export_pdf=self.settings.export_pdf,
            export_separate=self.settings.export_separate,
            export_merged=self.settings.export_merged,
            download_images=self.settings.download_images,
            save_metadata=self.settings.save_metadata,
            output_root=str(self.settings.resolved_output_dir()),
            conflict_policy="new_copy",
            merged_filename="所选内容" if self.settings.export_merged else "",
            expected_file_count=(plan_counts := offline_plan_counts(
                self.database,
                chosen_ids,
                self.settings.resolved_output_dir(),
                self.settings,
            ))[0],
            existing_target_count=plan_counts[1],
            target_count_unknown=False,
        )

        confirm_dlg = ConfirmPlanDialog(plan, self)
        if confirm_dlg.exec() != QDialog.DialogCode.Accepted or not confirm_dlg.confirmed:
            return

        export_dialog = OfflineExportDialog(
            self.database,
            self.settings,
            chosen_ids,
            parent=self,
            plan=plan,
        )
        export_dialog.exec()
        self.reload()


class OfflineExportDialog(QDialog):
    def __init__(
        self,
        database,
        settings: Settings,
        content_ids: list[int],
        parent=None,
        plan: ExportPlan | None = None,
    ):
        super().__init__(parent)
        self.database = database
        self.settings = settings
        self.content_ids = content_ids
        self.plan = plan
        self.controller = TaskController()
        self.worker = None

        self.setWindowTitle("离线重新导出")
        self.resize(540, 290)
        self.setFont(QFont("Microsoft YaHei", 9))

        self.markdown_box = QCheckBox("Markdown")
        self.docx_box = QCheckBox("Word")
        self.pdf_box = QCheckBox("PDF")
        self.separate_box = QCheckBox("每篇单独文件")
        self.merged_box = QCheckBox("合并导出")
        self.meta_box = QCheckBox("保存元数据")

        # 若由确认计划传入，直接使用计划参数
        if self.plan is not None:
            self.markdown_box.setChecked(self.plan.export_markdown)
            self.docx_box.setChecked(self.plan.export_docx)
            self.pdf_box.setChecked(self.plan.export_pdf)
            self.separate_box.setChecked(self.plan.export_separate)
            self.merged_box.setChecked(self.plan.export_merged)
            self.meta_box.setChecked(self.plan.save_metadata)
            output_str = self.plan.output_root
        else:
            self.markdown_box.setChecked(settings.export_markdown)
            self.docx_box.setChecked(settings.export_docx)
            self.pdf_box.setChecked(settings.export_pdf)
            self.separate_box.setChecked(settings.export_separate)
            self.merged_box.setChecked(settings.export_merged)
            self.meta_box.setChecked(settings.save_metadata)
            output_str = str(settings.resolved_output_dir())

        self.output_edit = QLineEdit(output_str)
        browse = QPushButton("…")
        browse.clicked.connect(self._choose)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit)
        output_row.addWidget(browse)

        formats = QHBoxLayout()
        for box in (self.markdown_box, self.docx_box, self.pdf_box, self.separate_box, self.merged_box, self.meta_box):
            formats.addWidget(box)

        self.progress = QProgressBar()
        self.status = QLabel(f"已选择 {len(content_ids)} 篇。导出只读取本地数据库。")
        self.start_button = QPushButton("开始导出")
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.start_button.clicked.connect(self._start)
        self.cancel_button.clicked.connect(self._cancel)

        actions = QHBoxLayout()
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addLayout(formats)
        layout.addLayout(output_row)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addLayout(actions)

        # 若计划已经确认且有效，直接开始执行
        if self.plan is not None:
            self._start()

    def _choose(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "输出目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def _start(self) -> None:
        if not any((self.markdown_box.isChecked(), self.docx_box.isChecked(), self.pdf_box.isChecked())):
            QMessageBox.warning(self, "离线重新导出", "请至少选择一种格式。")
            return
        if not any((self.separate_box.isChecked(), self.merged_box.isChecked())):
            QMessageBox.warning(self, "离线重新导出", "请选择单独文件或合并导出。")
            return

        conflict_policy = self.plan.conflict_policy if self.plan is not None else "new_copy"
        request = CollectRequest(
            url="offline",
            export_markdown=self.markdown_box.isChecked(),
            export_docx=self.docx_box.isChecked(),
            export_pdf=self.pdf_box.isChecked(),
            export_separate=self.separate_box.isChecked(),
            export_merged=self.merged_box.isChecked(),
            save_metadata=self.meta_box.isChecked(),
            output_dir=self.output_edit.text().strip(),
            conflict_policy=conflict_policy,
        )

        self.controller = TaskController()
        self.worker = _ExportWorker(
            self.database,
            self.content_ids,
            Path(request.output_dir),
            request,
            self.settings,
            self.controller,
            conflict_policy=conflict_policy,
        )
        self.worker.progress_changed.connect(self._on_progress)
        self.worker.finished_result.connect(self._on_finished)
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, max(1, len(self.content_ids)))
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
        self.status.setText("正在取消，已写完的文件会保留。")

    def _on_progress(self, done: int, total: int, title: str) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        self.status.setText(f"{done}/{total} {title}")

    def _on_finished(self, result) -> None:
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.status.setText(result.message)
        if getattr(self, "_parent_closing", False) or getattr(self, "_closing_connected", False):
            return
        if result.status == "success":
            QMessageBox.information(self, "离线重新导出", result.message)
        elif result.status == "cancelled":
            QMessageBox.information(self, "离线重新导出", result.message)
        else:
            QMessageBox.warning(self, "离线重新导出", result.message)


class _ExportWorker(QThread):
    progress_changed = Signal(int, int, str)
    finished_result = Signal(object)

    def __init__(self, database, content_ids, output_dir: Path, request, settings, controller, conflict_policy: str = "new_copy"):
        super().__init__()
        self.database = database
        self.content_ids = content_ids
        self.output_dir = output_dir
        self.request = request
        self.settings = settings
        self.controller = controller
        self.conflict_policy = conflict_policy

    def run(self) -> None:
        from zhihu_collector.collectors.offline import OfflineResult
        from zhihu_collector.collectors.runtime_lock import BUSY_MESSAGE, GATE

        if not GATE.acquire("offline-export"):
            self.finished_result.emit(OfflineResult("failed", 0, len(self.content_ids), BUSY_MESSAGE))
            return
        try:
            result = export_local_contents(
                self.database,
                self.content_ids,
                self.output_dir,
                self.request,
                self.settings,
                self.controller,
                on_progress=lambda done, total, title: self.progress_changed.emit(done, total, title),
            )
        except Exception as exc:
            result = OfflineResult("failed", 0, len(self.content_ids), f"导出异常：{exc}")
        finally:
            GATE.release()
        self.finished_result.emit(result)
