"""知乎内容搜集器主窗口。"""

from __future__ import annotations

import sqlite3

import copy
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zhihu_collector import __app_name__, __version__
from zhihu_collector.browser.auth_manager import AuthManager
from zhihu_collector.collectors.base import Progress, TaskController, progress_labels
from zhihu_collector.collectors.queue_session import QueueSession
from zhihu_collector.collectors.runtime_lock import BUSY_MESSAGE, GATE
from zhihu_collector.collectors.service import CollectRequest, classify_completion, collect_request_from_snapshot
from zhihu_collector.config import Settings, default_database_path, load_settings, save_settings
from zhihu_collector.database import catalog
from zhihu_collector.database.database import Database
from zhihu_collector.export_plan import ExportPlan
from zhihu_collector.gui.confirm_plan_dialog import ConfirmPlanDialog
from zhihu_collector.gui.failures_window import FailuresWindow
from zhihu_collector.gui.history_window import HistoryWindow
from zhihu_collector.gui.library_window import LibraryWindow
from zhihu_collector.gui.log_dialog import LogDialog
from zhihu_collector.gui.presets_dialog import PresetsDialog
from zhihu_collector.gui.settings_dialog import SettingsDialog
from zhihu_collector.gui.source_page import SourcePage
from zhihu_collector.gui.theme import apply_theme
from zhihu_collector.gui.worker import CollectWorker, LoginWorker
from zhihu_collector.presets import Preset, PresetManager
from zhihu_collector.utils.batch_targets import BatchTarget, identity_for, parse_batch
from zhihu_collector.utils.logger import get_logger
from zhihu_collector.utils.urls import parse_zhihu_url

logger = get_logger("gui.main")

_MODES = [
    ("auto", "自动识别（根据链接自动匹配）"),
    ("single_answer", "仅此回答（单篇回答）"),
    ("answers", "答主回答"),
    ("pins", "答主想法"),
    ("all", "答主全部内容"),
    ("question", "问题全部回答"),
    ("collection", "收藏夹回答（归档收藏夹全部回答）"),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(1280, 800)
        self.setMinimumSize(960, 640)
        self.setFont(QFont("Microsoft YaHei", 10))
        self.settings = load_settings()
        self.database = Database(self.settings.resolved_database_path())
        self.database.recover_interrupted_tasks()
        self.controller = TaskController()
        self.worker: CollectWorker | None = None
        self.login_worker: LoginWorker | None = None
        self._pending_database_path: Path | None = None
        self.preset_manager = PresetManager()
        self._loading_preset = False
        self._pending_collection_request: CollectRequest | None = None
        self.queue = QueueSession(self.database)
        self._run_token = None
        self._current_queue_id: int | None = None
        self._cancel_current = False
        self._last_finished_id: int | None = None
        self._batch_items: list[BatchTarget] = []

        self._build()
        self._load_presets_into_combo()
        self._refresh_login_label()
        self._set_running(False)

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        body = QHBoxLayout()
        nav = QVBoxLayout()
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.stack = QStackedWidget()
        pages = (
            ("采集", self._build_collect_page()),
            ("任务", self._build_task_page()),
            ("常用来源", None),
            ("内容库", None),
            ("设置", self._build_settings_page()),
        )
        self.library_page = LibraryWindow(self.database, self.settings, self)
        self.library_page.setWindowFlags(Qt.WindowType.Widget)
        self.source_page = SourcePage(
            self.database,
            self.preset_manager,
            self._enqueue_from_sources,
            self._open_source_library,
            self,
        )
        built = {
            "常用来源": self.source_page,
            "内容库": self.library_page,
        }
        for index, (label, page) in enumerate(pages):
            widget = built.get(label, page)
            self.stack.addWidget(widget)
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("role", "nav")
            if label == "内容库":
                self.library_button = button
            if label == "设置":
                self.settings_button = button
            button.clicked.connect(lambda _checked=False, page_index=index: self._show_page(page_index))
            self.nav_group.addButton(button)
            nav.addWidget(button)
            if index == 0:
                button.setChecked(True)
        nav.addStretch()
        nav_wrap = QWidget()
        nav_wrap.setLayout(nav)
        nav_wrap.setFixedWidth(148)
        body.addWidget(nav_wrap)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        self.status_label = QLabel("状态：就绪")
        self.batch_label = QLabel("整批：还没有队列")
        self.found_label = QLabel("已发现：0")
        self.completed_label = QLabel("已完成：0")
        self.current_label = QLabel("当前：")
        self.current_label.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.queue_notice = QLabel("")
        self.queue_notice.setWordWrap(True)
        outer.addWidget(self.status_label)
        outer.addWidget(self.batch_label)
        outer.addWidget(self.found_label)
        outer.addWidget(self.completed_label)
        outer.addWidget(self.progress)
        outer.addWidget(self.current_label)
        outer.addWidget(self.queue_notice)

        control_row = QHBoxLayout()
        self.start_queue_button = QPushButton("开始队列")
        self.pause_button = QPushButton("暂停队列")
        self.resume_button = QPushButton("继续队列")
        self.cancel_current_button = QPushButton("取消当前项")
        self.stop_button = QPushButton("停止队列")
        self.start_queue_button.clicked.connect(self._start_queue)
        self.pause_button.clicked.connect(self._pause_queue)
        self.resume_button.clicked.connect(self._resume_queue)
        self.cancel_current_button.clicked.connect(self._cancel_current_item)
        self.stop_button.clicked.connect(self._stop_queue)
        for button in (
            self.start_button,
            self.start_queue_button,
            self.pause_button,
            self.resume_button,
            self.cancel_current_button,
            self.stop_button,
        ):
            control_row.addWidget(button)
        outer.addLayout(control_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(200)
        self.log_view.setPlaceholderText("运行日志")
        self.log_view.setMaximumHeight(120)
        outer.addWidget(self.log_view)
        apply_theme(self)
        self._show_queue_recovery_notice()

    def _build_collect_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("采集")
        title.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        layout.addWidget(title)
        intro = QLabel("单个链接会在确认后加入队列。批量链接先识别，确认后才入队，不会马上联网。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://www.zhihu.com/people/xxxx、问题链接、收藏夹链接 或 单篇回答链接")
        self.url_edit.textChanged.connect(self._on_url_changed)
        self.url_edit.returnPressed.connect(self.start_collect)
        self.save_as_source_btn = QPushButton("保存为常用来源")
        self.save_as_source_btn.clicked.connect(self._save_current_as_source)
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.save_as_source_btn)
        form = QFormLayout()
        form.addRow("单个链接", url_row)
        layout.addLayout(form)

        preset_box = QGroupBox("常用采集配置")
        preset_layout = QHBoxLayout(preset_box)
        self.preset_combo = QComboBox()
        self.save_preset_btn = QPushButton("保存配置")
        self.manage_preset_btn = QPushButton("管理配置")
        self.preset_status_label = QLabel("")
        self.preset_status_label.setStyleSheet("color: #9a3412; font-size: 11px; background: transparent;")
        self.save_preset_btn.clicked.connect(self._save_current_preset)
        self.manage_preset_btn.clicked.connect(self._manage_presets)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        preset_layout.addWidget(QLabel("选择预设："))
        preset_layout.addWidget(self.preset_combo, 2)
        preset_layout.addWidget(self.save_preset_btn)
        preset_layout.addWidget(self.manage_preset_btn)
        preset_layout.addWidget(self.preset_status_label, 1)
        layout.addWidget(preset_box)

        self.mode_combo = QComboBox()
        for value, label in _MODES:
            self.mode_combo.addItem(label, value)
        self.mode_combo.currentIndexChanged.connect(self._mark_preset_modified)
        mode_row = QFormLayout()
        mode_row.addRow("类型", self.mode_combo)
        layout.addLayout(mode_row)

        limit_box = QGroupBox("每个目标的篇数上限")
        limit_layout = QHBoxLayout(limit_box)
        self.limit_group = QButtonGroup(self)
        self.limit_all_radio = QRadioButton("每个目标不限篇数")
        self.limit_sample_radio = QRadioButton("每个目标最多 5 篇")
        self.limit_custom_radio = QRadioButton("每个目标最多")
        self.limit_all_radio.setChecked(True)
        self.limit_group.addButton(self.limit_all_radio)
        self.limit_group.addButton(self.limit_sample_radio)
        self.limit_group.addButton(self.limit_custom_radio)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 10000)
        self.limit_spin.setValue(10)
        self.limit_spin.setEnabled(False)
        self.limit_spin.setSuffix(" 篇")
        self.limit_custom_radio.toggled.connect(self.limit_spin.setEnabled)
        self.limit_all_radio.toggled.connect(self._mark_preset_modified)
        self.limit_sample_radio.toggled.connect(self._mark_preset_modified)
        self.limit_custom_radio.toggled.connect(self._mark_preset_modified)
        self.limit_spin.valueChanged.connect(self._mark_preset_modified)
        limit_layout.addWidget(self.limit_all_radio)
        limit_layout.addWidget(self.limit_sample_radio)
        limit_layout.addWidget(self.limit_custom_radio)
        limit_layout.addWidget(self.limit_spin)
        limit_layout.addStretch()
        layout.addWidget(limit_box)
        limit_hint = QLabel("这里的数量是每个链接各自的上限，不是整批加起来只采这么多篇。未扫描前不预估篇数。")
        limit_hint.setWordWrap(True)
        layout.addWidget(limit_hint)

        intent_row = QHBoxLayout()
        self.intent_group = QButtonGroup(self)
        self.intent_export = QRadioButton("更新并导出")
        self.intent_library = QRadioButton("仅更新内容库")
        self.intent_export.setChecked(True)
        self.intent_group.addButton(self.intent_export)
        self.intent_group.addButton(self.intent_library)
        self.intent_export.toggled.connect(self._sync_intent_widgets)
        self.intent_library.toggled.connect(self._sync_intent_widgets)
        intent_row.addWidget(self.intent_export)
        intent_row.addWidget(self.intent_library)
        intent_row.addStretch()
        layout.addLayout(intent_row)

        export_row = QHBoxLayout()
        self.markdown_box = QCheckBox("Markdown")
        self.docx_box = QCheckBox("Word")
        self.pdf_box = QCheckBox("PDF")
        self.separate_box = QCheckBox("每篇单独文件")
        self.merged_box = QCheckBox("合并电子书")
        for box, checked in (
            (self.markdown_box, self.settings.export_markdown),
            (self.docx_box, self.settings.export_docx),
            (self.pdf_box, self.settings.export_pdf),
            (self.separate_box, self.settings.export_separate),
            (self.merged_box, self.settings.export_merged),
        ):
            box.setChecked(checked)
            box.toggled.connect(self._mark_preset_modified)
            export_row.addWidget(box)
        layout.addLayout(export_row)

        option_row = QHBoxLayout()
        self.image_box = QCheckBox("下载正文图片")
        self.meta_box = QCheckBox("保存元数据")
        self.update_only_box = QCheckBox("旧式提前结束（仅旧任务）")
        self.image_box.setChecked(self.settings.download_images)
        self.meta_box.setChecked(self.settings.save_metadata)
        self.image_box.toggled.connect(self._mark_preset_modified)
        self.meta_box.toggled.connect(self._mark_preset_modified)
        self.update_only_box.toggled.connect(self._mark_preset_modified)
        option_row.addWidget(self.image_box)
        option_row.addWidget(self.meta_box)
        option_row.addWidget(self.update_only_box)
        layout.addLayout(option_row)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.settings.resolved_output_dir()))
        self.output_edit.textChanged.connect(self._mark_preset_modified)
        self.remember_output_check = QCheckBox("此配置记住输出目录")
        self.remember_output_check.toggled.connect(self._mark_preset_modified)
        browse = QPushButton("…")
        browse.clicked.connect(self._choose_output)
        output_row.addWidget(self.output_edit)
        output_row.addWidget(browse)
        output_form = QFormLayout()
        output_form.addRow("输出目录", output_row)
        output_form.addRow("", self.remember_output_check)
        layout.addLayout(output_form)

        self.start_button = QPushButton("开始搜集")
        self.start_button.setProperty("role", "primary")
        self.start_button.clicked.connect(self.start_collect)

        batch_box = QGroupBox("批量链接")
        batch_layout = QVBoxLayout(batch_box)
        self.batch_edit = QPlainTextEdit()
        self.batch_edit.setPlaceholderText("每行一个链接，或一行分享文字里带一个知乎链接")
        self.batch_edit.setMaximumHeight(120)
        self.batch_edit.textChanged.connect(self._on_batch_text_changed)
        recognize = QPushButton("识别链接")
        recognize.clicked.connect(self._recognize_batch)
        self.batch_summary = QLabel("还没有识别。")
        self.batch_summary.setWordWrap(True)
        self.batch_table = QTableWidget(0, 6)
        self.batch_table.setHorizontalHeaderLabels(["原输入", "规范化目标", "类型", "范围", "有效性", "说明"])
        self.enqueue_button = QPushButton("加入队列")
        self.enqueue_button.clicked.connect(self._enqueue_batch)
        batch_layout.addWidget(self.batch_edit)
        batch_layout.addWidget(recognize)
        batch_layout.addWidget(self.batch_summary)
        batch_layout.addWidget(self.batch_table)
        batch_layout.addWidget(self.enqueue_button)
        layout.addWidget(batch_box)
        layout.addStretch()
        self._sync_intent_widgets()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _build_task_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("任务")
        title.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        layout.addWidget(title)
        hint = QLabel("同一时间只跑一个联网任务。等待项可以删除或调整顺序；正在执行的项不能直接删掉。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setHorizontalHeaderLabels(["顺序", "目标", "状态", "说明", "批次"])
        self.queue_table.horizontalHeader().setStretchLastSection(True)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.queue_table)
        row = QHBoxLayout()
        view_content_btn = QPushButton("查看本次内容")
        view_content_btn.setProperty("role", "primary")
        open_output_btn = QPushButton("打开输出目录")
        task_detail_btn = QPushButton("查看结果详情")
        up = QPushButton("上移等待项")
        down = QPushButton("下移等待项")
        delete = QPushButton("删除等待项")
        retry = QPushButton("重试失败项")
        history_button = QPushButton("任务历史")
        failures_button = QPushButton("失败项")
        log_button = QPushButton("查看日志")
        view_content_btn.clicked.connect(self._view_selected_task_content)
        open_output_btn.clicked.connect(self._open_selected_task_output)
        task_detail_btn.clicked.connect(self._view_selected_task_detail)
        up.clicked.connect(lambda: self._move_queue(-1))
        down.clicked.connect(lambda: self._move_queue(1))
        delete.clicked.connect(self._delete_waiting_item)
        retry.clicked.connect(self._retry_failed_item)
        history_button.clicked.connect(self.show_history)
        failures_button.clicked.connect(self.show_failures)
        log_button.clicked.connect(lambda: LogDialog(self).exec())
        self.queue_table.doubleClicked.connect(lambda: self._view_selected_task_detail())
        for button in (
            view_content_btn, open_output_btn, task_detail_btn,
            up, down, delete, retry, history_button, failures_button, log_button,
        ):
            row.addWidget(button)
        layout.addLayout(row)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("设置")
        title.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
        layout.addWidget(title)
        self.login_button = QPushButton("登录知乎")
        self.login_button.clicked.connect(self.start_login)
        self.login_label = QLabel()
        self.login_label.setWordWrap(True)
        detail = QPushButton("采集节奏、数据库和输出目录")
        detail.clicked.connect(self.show_settings)
        note = QLabel("队列里还有等待、暂停或中断的项目时，不能把数据库换到另一份文件，避免同一批任务写进两个库。")
        note.setWordWrap(True)
        layout.addWidget(self.login_button)
        layout.addWidget(self.login_label)
        layout.addWidget(detail)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        button = self.nav_group.buttons()[index]
        button.setChecked(True)
        if index == 2:
            self.source_page.database = self.database
            self.source_page.reload()
        if index == 3:
            self.library_page.database = self.database
            self.library_page.settings = self.settings
            self.library_page._reload_collections()
            self.library_page.reload()
        if index == 1:
            self._refresh_queue_table()

    def _show_queue_recovery_notice(self) -> None:
        counts = catalog.queue_counts(self.database)
        needs_auth = counts.get("needs_login", 0) + counts.get("needs_verify", 0)
        interrupted = counts.get("interrupted", 0)
        if needs_auth:
            self.queue_notice.setText(
                f"队列中有 {needs_auth} 项需要登录或验证。请处理后点击「继续队列」。"
            )
        elif interrupted:
            self.queue_notice.setText(
                f"有 {interrupted} 项上次未完成，已标为已中断。程序不会自动联网，点「继续队列」后才会继续。"
            )
        else:
            self.queue_notice.setText("")
        self._refresh_queue_table()

    def _on_url_changed(self, text: str) -> None:
        parsed = parse_zhihu_url(text.strip())
        if parsed.kind == "collection":
            self._set_mode("collection")
            self.start_button.setText("读取收藏夹清单并开始搜集")
        elif parsed.kind == "answer":
            self._set_mode("single_answer")
            self.start_button.setText("开始搜集")
        else:
            self.start_button.setText("开始搜集")

    def _save_current_as_source(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "保存为常用来源", "请先输入知乎链接。")
            return
        parsed = parse_zhihu_url(url)
        identity, mode, error = identity_for(parsed, self._selected_mode())
        if error or not identity:
            QMessageBox.warning(self, "保存为常用来源", error or "这不是可识别的知乎答主、问题、收藏夹或单篇回答链接。")
            return
        preset_id = self.preset_combo.currentData() or ""
        preset = self.preset_manager.get_preset(preset_id)
        snapshot = preset.to_dict() if preset is not None else self._request_from_form().snapshot()
        source_id, created = catalog.save_source(
            self.database,
            display_name=parsed.canonical,
            canonical_url=parsed.canonical,
            target_kind=parsed.kind,
            identity_key=identity,
            collect_mode=mode,
            preset_id=preset_id,
            preset_snapshot=snapshot,
        )
        if not created:
            QMessageBox.information(self, "保存为常用来源", f"该目标与采集范围已存在于常用来源中（ID: {source_id}）。")
        else:
            QMessageBox.information(
                self,
                "保存为常用来源",
                f"已成功保存为常用来源：\n{parsed.canonical}\n范围：{mode}\n可在「常用来源」页面中查看并管理。",
            )
        self.source_page.reload()

    def _mark_preset_modified(self) -> None:
        if self._loading_preset:
            return
        self.preset_status_label.setText("[已修改，尚未保存]")

    def _load_presets_into_combo(self) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        target_idx = 0
        for idx, p in enumerate(self.preset_manager.presets):
            self.preset_combo.addItem(p.name, p.id)
            if p.id == self.preset_manager.last_selected_id:
                target_idx = idx
        self.preset_combo.setCurrentIndex(target_idx)
        self.preset_combo.blockSignals(False)
        if self.preset_manager.presets:
            self._apply_preset_to_form(self.preset_manager.presets[target_idx])

    def _on_preset_selected(self, index: int) -> None:
        if index < 0 or index >= len(self.preset_manager.presets):
            return
        preset = self.preset_manager.presets[index]
        try:
            self.preset_manager.select_preset(preset.id)
        except OSError as exc:
            self._load_presets_into_combo()
            QMessageBox.warning(self, "配置未切换", f"无法保存所选配置：{exc}")
            return
        self._apply_preset_to_form(preset)

    def _apply_preset_to_form(self, preset: Preset) -> None:
        self._loading_preset = True
        try:
            self.markdown_box.setChecked(preset.export_markdown)
            self.docx_box.setChecked(preset.export_docx)
            self.pdf_box.setChecked(preset.export_pdf)
            self.separate_box.setChecked(preset.export_separate)
            self.merged_box.setChecked(preset.export_merged)
            self.image_box.setChecked(preset.download_images)
            self.meta_box.setChecked(preset.save_metadata)
            self.update_only_box.setChecked(preset.update_only)
            if getattr(preset, "execution_intent", "") == "library":
                self.intent_library.setChecked(True)
            else:
                self.intent_export.setChecked(True)
            self._sync_intent_widgets()

            if preset.max_items == 0:
                self.limit_all_radio.setChecked(True)
            elif preset.max_items == 5:
                self.limit_sample_radio.setChecked(True)
            else:
                self.limit_custom_radio.setChecked(True)
                self.limit_spin.setValue(preset.max_items)

            if preset.remember_output_dir and preset.output_dir:
                self.output_edit.setText(preset.output_dir)
            self.remember_output_check.setChecked(preset.remember_output_dir)

            self.preset_status_label.setText("")
        finally:
            self._loading_preset = False

    def _preset_from_form(self, preset_id: str, name: str) -> Preset:
        req = self._request_from_form()
        return Preset(
            id=preset_id,
            name=name,
            export_markdown=req.export_markdown,
            export_docx=req.export_docx,
            export_pdf=req.export_pdf,
            export_separate=req.export_separate,
            export_merged=req.export_merged,
            download_images=req.download_images,
            save_metadata=req.save_metadata,
            update_only=req.update_only,
            execution_intent=req.execution_intent,
            max_items=req.max_items,
            remember_output_dir=self.remember_output_check.isChecked(),
            output_dir=req.output_dir if self.remember_output_check.isChecked() else "",
        )

    def _commit_preset(self, preset: Preset, success_message: str) -> bool:
        try:
            preset.validate()
            self.preset_manager.save_or_update(preset)
        except ValueError as exc:
            QMessageBox.warning(self, "保存配置", str(exc))
            return False
        except OSError as exc:
            QMessageBox.warning(self, "保存配置", f"配置文件没有写入成功：{exc}")
            return False
        self._load_presets_into_combo()
        self.preset_status_label.setText("")
        QMessageBox.information(self, "保存配置", success_message)
        return True

    def _save_current_preset(self) -> None:
        current_id = self.preset_combo.currentData()
        current = self.preset_manager.get_preset(current_id)
        is_builtin = current and current.id in ("daily_archive", "merged_reading", "small_sample")

        if is_builtin or current is None:
            name, ok = QInputDialog.getText(
                self, "另存为新配置", "内置模板不能直接覆盖，请输入新配置名称："
            )
            if not ok or not str(name).strip():
                return
            name = str(name).strip()
            if any(p.name == name for p in self.preset_manager.presets):
                QMessageBox.warning(self, "保存配置", f"已存在名为「{name}」的配置，请使用不同的名称。")
                return
            new_preset = self._preset_from_form(f"custom_{uuid.uuid4().hex[:8]}", name)
            self._commit_preset(new_preset, f"已另存为新配置「{name}」。")
            return

        box = QMessageBox(self)
        box.setWindowTitle("保存配置")
        box.setText(f"是否更新覆盖现有配置「{current.name}」，还是另存为新配置？")
        overwrite_btn = box.addButton("更新当前配置", QMessageBox.ButtonRole.AcceptRole)
        save_as_btn = box.addButton("另存为新配置", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is overwrite_btn:
            updated = replace(current)
            drafted = self._preset_from_form(current.id, current.name)
            updated = replace(
                updated,
                export_markdown=drafted.export_markdown,
                export_docx=drafted.export_docx,
                export_pdf=drafted.export_pdf,
                export_separate=drafted.export_separate,
                export_merged=drafted.export_merged,
                download_images=drafted.download_images,
                save_metadata=drafted.save_metadata,
                update_only=drafted.update_only,
                execution_intent=drafted.execution_intent,
                max_items=drafted.max_items,
                remember_output_dir=drafted.remember_output_dir,
                output_dir=drafted.output_dir,
            )
            self._commit_preset(updated, f"配置「{current.name}」已更新。")
        elif clicked is save_as_btn:
            name, ok = QInputDialog.getText(
                self, "另存为新配置", "请输入新配置名称：", text=f"{current.name} - 副本"
            )
            if not ok or not str(name).strip():
                return
            name = str(name).strip()
            if any(p.name == name for p in self.preset_manager.presets):
                QMessageBox.warning(self, "保存配置", f"已存在名为「{name}」的配置，请使用不同的名称。")
                return
            new_preset = self._preset_from_form(f"custom_{uuid.uuid4().hex[:8]}", name)
            self._commit_preset(new_preset, f"已另存为新配置「{name}」。")

    def _manage_presets(self) -> None:
        dialog = PresetsDialog(self.preset_manager, self)
        dialog.exec()
        self._load_presets_into_combo()

    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "输出目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def _set_mode(self, mode: str) -> None:
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def _selected_mode(self) -> str:
        return str(self.mode_combo.currentData() or "auto")

    def _request_from_form(self) -> CollectRequest:
        max_items = 0
        if self.limit_sample_radio.isChecked():
            max_items = 5
        elif self.limit_custom_radio.isChecked():
            max_items = self.limit_spin.value()

        return CollectRequest(
            url=self.url_edit.text().strip(),
            mode=self._selected_mode(),
            output_dir=self.output_edit.text().strip(),
            download_images=self.image_box.isChecked(),
            save_metadata=self.meta_box.isChecked(),
            export_markdown=self.markdown_box.isChecked(),
            export_docx=self.docx_box.isChecked(),
            export_pdf=self.pdf_box.isChecked(),
            export_separate=self.separate_box.isChecked(),
            export_merged=self.merged_box.isChecked(),
            update_only=self.update_only_box.isChecked(),
            max_items=max_items,
            execution_intent="library" if self.intent_library.isChecked() else "export",
        )

    def start_collect(self) -> None:
        request = self._request_from_form()
        if not request.url:
            QMessageBox.warning(self, "知乎内容搜集器", "请先输入知乎链接。")
            return
        if request.execution_intent != "library":
            if not any((request.export_markdown, request.export_docx, request.export_pdf)):
                QMessageBox.warning(self, "知乎内容搜集器", "更新并导出时请至少选择一种导出格式（Markdown、Word 或 PDF）。")
                return
            if not (request.export_separate or request.export_merged):
                QMessageBox.warning(self, "知乎内容搜集器", "更新并导出时请至少勾选「每篇单独文件」或「合并电子书」其中一项。")
                return
        parsed = parse_zhihu_url(request.url)
        if parsed.kind == "unknown":
            QMessageBox.warning(
                self,
                "知乎内容搜集器",
                "这不是可识别的知乎答主、问题、收藏夹或回答链接。\n示例：\n"
                "- 收藏夹：https://www.zhihu.com/collection/123456\n"
                "- 答主：https://www.zhihu.com/people/xxx\n"
                "- 问题：https://www.zhihu.com/question/123\n"
                "- 回答：https://www.zhihu.com/question/123/answer/456 或 /answer/456",
            )
            return
        if request.mode == "single_answer" and parsed.kind != "answer":
            QMessageBox.warning(
                self,
                "知乎内容搜集器",
                "「仅此回答」模式需要输入具体的回答链接（格式：.../question/xxx/answer/yyy 或 .../answer/yyy）。",
            )
            return
        if request.mode == "collection" and parsed.kind != "collection":
            QMessageBox.warning(
                self,
                "知乎内容搜集器",
                "「收藏夹回答」模式需要输入知乎收藏夹链接（格式：https://www.zhihu.com/collection/xxx）。",
            )
            return

        # 弹出采集计划确认对话框
        source_title = parsed.canonical
        source_id = ""
        if parsed.kind == "collection":
            request.collection_scope_confirmation = True
            source_title = f"知乎收藏夹 (ID: {parsed.collection_id})"
            source_id = parsed.collection_id or ""
        elif parsed.kind == "user":
            source_title = f"答主主页 ({parsed.user_token})"
            source_id = parsed.user_token or ""
        elif parsed.kind == "question":
            source_title = f"知乎问题 (ID: {parsed.question_id})"
            source_id = parsed.question_id or ""
        elif parsed.kind == "answer":
            source_title = f"单篇回答 (ID: {parsed.answer_id})"
            source_id = parsed.answer_id or ""

        if request.execution_intent == "library":
            limit_text = "不限篇数" if request.max_items == 0 else f"每个目标最多 {request.max_items} 篇"
            answer = QMessageBox.question(
                self,
                "加入队列",
                f"仅更新内容库，不生成 Markdown、Word 或 PDF。\n目标：{parsed.canonical}\n{limit_text}\n图片："
                + ("下载" if request.download_images else "不下载"),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        else:
            plan = ExportPlan(
                scope_kind="network_plan",
                source_title=source_title,
                source_url=parsed.canonical,
                source_id=source_id,
                export_markdown=request.export_markdown,
                export_docx=request.export_docx,
                export_pdf=request.export_pdf,
                export_separate=request.export_separate,
                export_merged=request.export_merged,
                download_images=request.download_images,
                save_metadata=request.save_metadata,
                output_root=request.output_dir or str(self.settings.resolved_output_dir()),
                conflict_policy="new_copy",
                max_items=request.max_items,
            )
            confirm_dlg = ConfirmPlanDialog(plan, self)
            if confirm_dlg.exec() != QDialog.DialogCode.Accepted or not confirm_dlg.confirmed:
                return
            request.conflict_policy = plan.conflict_policy

        if parsed.kind == "collection":
            request.collection_scope_confirmation = True
        self._enqueue_requests([request])
        self._start_queue()

    def start_request(self, request: CollectRequest) -> None:
        self.url_edit.setText(request.url)
        if request.output_dir:
            self.output_edit.setText(request.output_dir)
        self._enqueue_requests([request])
        self._start_queue()

    def _ask_existing(self, existing) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("检测到历史任务")
        box.setText(f"检测到历史任务\n\n已完成：{existing['total_completed'] or 0}\n继续搜集剩余内容")
        resume_button = box.addButton("继续搜集", QMessageBox.ButtonRole.AcceptRole)
        update_button = box.addButton("只更新新增", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is resume_button:
            return "resume"
        if clicked is update_button:
            return "update"
        return "cancel"

    def _sync_intent_widgets(self) -> None:
        library = self.intent_library.isChecked()
        for box in (self.markdown_box, self.docx_box, self.pdf_box, self.separate_box, self.merged_box):
            box.setEnabled(not library)

    def _accept_token(self, token, *, finalize: bool = False) -> bool:
        if token is None:
            return True
        if token != self._run_token:
            return False
        if finalize:
            self._run_token = None
        return True

    def _enqueue_requests(self, requests: list[CollectRequest], source_ids: list[int] | None = None) -> list[int]:
        items = []
        for index, request in enumerate(requests):
            parsed = parse_zhihu_url(request.url)
            identity, mode, error = identity_for(parsed, request.mode or "auto")
            if error or not identity:
                self._append_log(error or "跳过无法识别的目标")
                continue
            request.mode = mode
            if source_ids is not None:
                request.source_id = source_ids[index]
            items.append({
                "request_json": json.dumps(request.snapshot(), ensure_ascii=False),
                "dedupe_key": f"{identity}|{mode}",
                "source_id": request.source_id,
            })
        if not items:
            return []
        ids = catalog.enqueue_requests(self.database, items, batch_id=uuid.uuid4().hex[:8])
        skipped = len(items) - len(ids)
        if skipped > 0:
            self._append_log(f"已加入队列 {len(ids)} 项。跳过 {skipped} 项已在队列中的重复任务。")
        else:
            self._append_log(f"已加入队列 {len(ids)} 项。之后修改配置不会改变这些任务。")
        self._refresh_queue_table()
        return ids

    def _enqueue_from_sources(self, requests, source_ids) -> None:
        self._enqueue_requests(requests, source_ids)
        self._show_page(1)
        self._start_queue()

    def _open_source_library(self, source_id: int) -> None:
        self.library_page._fixed_source_id = None
        self.library_page._task_id = None
        self.library_page._task_outcome = ""
        self.library_page.task_filter_bar.setVisible(False)
        self._show_page(3)
        index = self.library_page.source_combo.findData(source_id)
        if index >= 0:
            self.library_page.source_combo.setCurrentIndex(index)
        self.library_page.search_first()

    def _view_selected_task_content(self) -> None:
        row = self._selected_queue_row()
        if row is None:
            QMessageBox.information(self, "查看本次内容", "请先在上方表格中选择一个已执行的任务。")
            return
        task_id = row["task_id"]
        if not task_id:
            QMessageBox.information(self, "查看本次内容", "该队列项尚未执行完成或没有关联采集任务。")
            return
        self._open_task_library(int(task_id))

    def _open_task_library(self, task_id: int) -> None:
        self.library_page._fixed_source_id = None
        self.library_page._task_id = task_id
        self.library_page._task_outcome = ""
        self._show_page(3)
        self.library_page.task_filter_label.setText(f"🔍 当前筛选：任务 #{task_id}")
        self.library_page.task_outcome_combo.blockSignals(True)
        self.library_page.task_outcome_combo.setCurrentIndex(0)
        self.library_page.task_outcome_combo.blockSignals(False)
        self.library_page.task_filter_bar.setVisible(True)
        self.library_page.search_first()

    def _open_selected_task_output(self) -> None:
        row = self._selected_queue_row()
        if row is None:
            QMessageBox.information(self, "打开输出目录", "请先在上方表格中选择一个任务。")
            return
        try:
            req_data = json.loads(row["request_json"] or "{}")
        except json.JSONDecodeError:
            req_data = {}
        if req_data.get("execution_intent") == "library":
            QMessageBox.information(
                self,
                "打开输出目录",
                "该任务为「仅更新内容库」，不生成 Markdown、Word 或 PDF 文件。\n内容已保存在本地数据库中，可在「内容库」中阅读。",
            )
            return
        output_dir = req_data.get("output_dir")
        if not output_dir and row["task_id"]:
            task = self.database.get_task(int(row["task_id"]))
            if task is not None:
                output_dir = task["output_dir"]
        if not output_dir:
            output_dir = str(self.settings.resolved_output_dir())
        path = Path(output_dir).resolve()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.warning(self, "打开输出目录", f"输出目录不存在或尚未生成：\n{path}")

    def _view_selected_task_detail(self) -> None:
        row = self._selected_queue_row()
        if row is None:
            return
        try:
            req_data = json.loads(row["request_json"] or "{}")
        except json.JSONDecodeError:
            req_data = {}
        try:
            res_data = json.loads(row["result_json"] or "{}")
        except json.JSONDecodeError:
            res_data = {}
        status_label = catalog.QUEUE_STATUS_LABELS.get(row["status"], row["status"])
        intent_label = "仅更新内容库" if req_data.get("execution_intent") == "library" else "更新并导出"

        detail_text = (
            f"目标链接：{req_data.get('url', '未知')}\n"
            f"采集模式：{req_data.get('mode', '自动')}\n"
            f"执行方式：{intent_label}\n"
            f"当前状态：{status_label}\n"
            f"队列序号：第 {row['position']} 项（批次：{row['batch_id'] or '无'}）\n"
            f"创建时间：{row['created_at'] or '未知'}\n"
            f"开始时间：{row['started_at'] or '未开始'}\n"
            f"结束时间：{row['finished_at'] or '未结束'}\n"
        )
        if row["task_id"]:
            detail_text += f"关联任务：ID #{row['task_id']}\n"
        if row["error_message"]:
            detail_text += f"\n错误说明：{row['error_message']}\n"
        if res_data:
            detail_text += (
                f"\n结果统计：\n"
                f"- 扫描篇数：{res_data.get('scanned', 0)}\n"
                f"- 新增入库：{res_data.get('saved_new', 0)}\n"
                f"- 正文更新：{res_data.get('body_updated', 0)}\n"
                f"- 复用旧文：{res_data.get('reused', 0)}\n"
                f"- 失败数量：{res_data.get('failed', 0)}\n"
            )
        QMessageBox.information(self, "任务详情", detail_text)

    def _start_queue(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self._append_log("队列已在执行，新项目会排在后面。")
            return
        counts = catalog.queue_counts(self.database)
        if counts.get("needs_login", 0) > 0 or counts.get("needs_verify", 0) > 0:
            self.queue.hold = True
            self._append_log("队列中有项目需要登录或验证，请在解决后点击「继续队列」。")
            self._refresh_queue_table()
            return
        self.queue.hold = False
        self._advance_queue()

    def _pause_queue(self) -> None:
        self.queue.hold = True
        self.controller.pause()
        self._append_log("队列已暂停。当前项停在安全检查点，后面的项目不会开始。")
        self._refresh_queue_table()

    def _resume_queue(self) -> None:
        self.queue.hold = False
        self.controller.resume()
        if self.worker is not None and self.worker.isRunning():
            self._append_log("已继续当前项。")
            return
        catalog.reopen_queue_statuses(self.database, ("interrupted", "needs_login", "needs_verify"))
        self.queue_notice.setText("")
        self._append_log("继续队列。")
        self._advance_queue()

    def _stop_queue(self) -> None:
        self.queue.hold = True
        self._cancel_current = False
        self.controller.stop()
        self.queue_notice.setText("正在停止当前项并关闭浏览器，已保存内容保留。")
        self._append_log("正在停止队列。当前项正在结束，等待项保留。")

    def _cancel_current_item(self) -> None:
        self._cancel_current = True
        self.queue.hold = False
        self.controller.stop()
        self.queue_notice.setText("正在取消当前项并关闭浏览器。")
        self._append_log("只取消当前项，其余等待项会继续。")

    def _advance_queue(self) -> None:
        if self.queue.hold:
            self._refresh_queue_table()
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if GATE.busy:
            self._append_log("已有采集或导出正在写入，队列暂时不开始下一项。")
            self._refresh_queue_table()
            return
        item = self.queue.try_begin()
        if item is None:
            self._refresh_queue_table()
            return
        request = collect_request_from_snapshot(json.loads(item["request_json"] or "{}"))
        request.queue_item_id = int(item["id"])
        self._current_queue_id = int(item["id"])
        self._launch(request)

    def _finish_current_from_task(self) -> str:
        item_id = self._current_queue_id
        self._last_finished_id = item_id
        if item_id is None:
            return "success"
        status = "failed"
        row = next((item for item in catalog.list_queue_items(self.database) if int(item["id"]) == int(item_id)), None)
        task_id = row["task_id"] if row is not None else None
        result = {}
        error_msg = ""
        if task_id:
            task = self.database.get_task(int(task_id))
            raw = task["status"] if task is not None else "failed"
            error_msg = str(task["error_message"] or "") if task is not None else ""
            status = {
                "completed": "success",
                "partial": "partial",
                "unconfirmed": "unconfirmed",
                "failed": "failed",
                "needs_login": "needs_login",
                "needs_verification": "needs_verify",
                "interrupted": "interrupted",
                "awaiting_confirmation": "awaiting_confirm",
            }.get(raw, "failed")
            inserted_count = len(catalog.task_content_ids(self.database, int(task_id), "inserted"))
            updated_count = len(catalog.task_content_ids(self.database, int(task_id), "updated"))
            reused_count = len(catalog.task_content_ids(self.database, int(task_id), "reused")) + len(
                catalog.task_content_ids(self.database, int(task_id), "metadata")
            )
            # 失败数只用采集过程实际记下的数字；参考总数与完成数之差可能是上限或网站差额，不算失败
            try:
                noted = json.loads(row["result_json"] or "{}")
            except json.JSONDecodeError:
                noted = {}
            if "failed" in noted:
                failed_count = int(noted.get("failed") or 0)
            else:
                failed_count = catalog.task_open_body_failures(self.database, int(task_id))
            result = {
                "scanned": task["total_found"] or 0,
                "saved_new": inserted_count,
                "updated": updated_count,
                "body_updated": updated_count,
                "reused": reused_count,
                "failed": failed_count,
            }
            for key in ("images_failed", "exports_failed"):
                if key in noted:
                    result[key] = int(noted.get(key) or 0)
        self.queue.finish(item_id, status, error_msg, result)
        self._current_queue_id = None
        return status

    def _refresh_queue_table(self) -> None:
        if not hasattr(self, "queue_table"):
            return
        rows = catalog.list_queue_items(self.database)
        counts = catalog.queue_counts(self.database)
        finished = sum(counts.get(name, 0) for name in ("success", "partial", "unconfirmed", "failed", "cancelled"))
        total = len(rows)
        self.batch_label.setText(
            f"整批已结束 {finished}/{total}。成功 {counts.get('success', 0)}，"
            f"部分完成 {counts.get('partial', 0)}，完整性未确认 {counts.get('unconfirmed', 0)}，"
            f"失败 {counts.get('failed', 0)}，已取消 {counts.get('cancelled', 0)}。"
        )
        self.queue_table.setRowCount(len(rows))
        self._queue_rows = rows
        for index, row in enumerate(rows):
            try:
                payload = json.loads(row["request_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            label = catalog.QUEUE_STATUS_LABELS.get(row["status"], row["status"])
            if self.queue.hold and row["status"] == "running":
                label = "已暂停"
            values = [
                str(row["position"]),
                payload.get("url") or "",
                label,
                row["error_message"] or "",
                row["batch_id"] or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.queue_table.setItem(index, column, item)

    def _selected_queue_row(self):
        indexes = self.queue_table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self._queue_rows[indexes[0].row()]

    def _move_queue(self, direction: int) -> None:
        row = self._selected_queue_row()
        if row is None or row["status"] != "waiting":
            self._append_log("只能调整等待中的项目。")
            return
        catalog.move_waiting_item(self.database, int(row["id"]), direction)
        self._refresh_queue_table()

    def _delete_waiting_item(self) -> None:
        row = self._selected_queue_row()
        if row is None or row["status"] != "waiting":
            self._append_log("只能删除等待中的项目。")
            return
        catalog.delete_waiting_item(self.database, int(row["id"]))
        self._refresh_queue_table()

    def _retry_failed_item(self) -> None:
        row = self._selected_queue_row()
        if row is None or row["status"] != "failed":
            self._append_log("只能重试失败项，成功项不会跟着再跑。")
            return
        if not catalog.retry_failed_item(self.database, int(row["id"])):
            self._append_log("同一目标已在队列中，没有重复加入。")
        self._refresh_queue_table()

    def _on_batch_text_changed(self) -> None:
        if self._batch_items:
            self._batch_items = []
            self.batch_summary.setText("输入内容已修改，请点击「识别链接」重新核对后再入队。")
            self.batch_table.setRowCount(0)

    def _recognize_batch(self) -> None:
        request = self._request_from_form()
        items = parse_batch(
            self.batch_edit.toPlainText(),
            default_mode=self._selected_mode(),
            active_keys=catalog.active_queue_by_key(self.database),
            option_signature={"max_items": request.max_items, "execution_intent": request.execution_intent},
        )
        self._batch_items = items
        self.batch_table.setRowCount(len(items))
        valid = sum(1 for item in items if item.valid)
        invalid = sum(1 for item in items if not item.valid and item.reason != "空行")
        blanks = sum(1 for item in items if item.reason == "空行")
        self.batch_summary.setText(f"可入队 {valid} 项，无效或重复 {invalid} 项，空行 {blanks} 行。识别不会开始采集。")
        for index, item in enumerate(items):
            values = [
                item.raw,
                item.canonical,
                item.type_label,
                "",
                "有效" if item.valid else "无效",
                item.reason,
            ]
            for column, value in enumerate(values):
                if column == 3:
                    continue
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.batch_table.setItem(index, column, cell)
            combo = QComboBox()
            for mode, label in _MODES:
                if mode == "auto":
                    continue
                combo.addItem(label, mode)
            if item.mode:
                found = combo.findData(item.mode)
                if found >= 0:
                    combo.setCurrentIndex(found)
            combo.setEnabled(item.parsed is not None and item.kind != "unknown")
            self.batch_table.setCellWidget(index, 3, combo)

    def _enqueue_batch(self) -> None:
        if not self._batch_items:
            self._recognize_batch()
        requests = []
        skipped = 0
        for index, item in enumerate(self._batch_items):
            combo = self.batch_table.cellWidget(index, 3)
            mode = combo.currentData() if combo is not None else item.mode
            if item.parsed is None:
                skipped += 1
                continue
            identity, resolved, error = identity_for(item.parsed, mode or item.mode or "auto")
            if error or not identity:
                skipped += 1
                continue
            key = f"{identity}|{resolved}"
            if key in catalog.active_queue_by_key(self.database):
                skipped += 1
                continue
            if any(getattr(existing, "_dedupe_key", "") == key for existing in requests):
                skipped += 1
                continue
            request = self._request_from_form()
            request.url = item.canonical
            request.mode = resolved
            if resolved == "collection":
                request.collection_scope_confirmation = True
            request._dedupe_key = key
            requests.append(request)
        if not requests:
            QMessageBox.information(self, "批量链接", "没有新的有效目标可以入队。")
            return
        limit_text = "每个目标不限篇数" if requests[0].max_items == 0 else f"每个目标最多 {requests[0].max_items} 篇"
        intent = "仅更新内容库，不生成导出文件" if requests[0].execution_intent == "library" else "更新并导出"
        policy_label = {
            "new_copy": "创建新副本",
            "overwrite": "覆盖同名文件",
            "skip": "跳过已有文件",
        }.get(requests[0].conflict_policy, requests[0].conflict_policy)
        conflict_text = f"冲突策略：{policy_label}\n" if requests[0].execution_intent != "library" else ""
        answer = QMessageBox.question(
            self,
            "加入队列",
            f"有效任务 {len(requests)} 项，另有 {skipped} 行不会入队。\n{intent}。{limit_text}。\n"
            f"{conflict_text}输出目录：{requests[0].output_dir or self.settings.resolved_output_dir()}\n"
            "未扫描前不能预估篇数。确认后只入队，点「开始队列」才会联网。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for request in requests:
            if hasattr(request, "_dedupe_key"):
                delattr(request, "_dedupe_key")
        self._enqueue_requests(requests)
        self._show_page(1)

    def _launch(self, request: CollectRequest) -> None:
        if self.worker is not None and self.worker.isRunning():
            self._append_log("当前项还在执行，没有启动第二个任务。")
            return
        if request.execution_intent != "library":
            self._remember_export_choices(request)
        self.controller = TaskController()
        settings_snapshot = copy.deepcopy(self.settings)
        token = object()
        self._run_token = token
        self.worker = CollectWorker(settings_snapshot, self.database, request, self.controller)
        self.worker.progress_changed.connect(self._on_progress)
        self.worker.log_line.connect(self._append_log)
        self.worker.succeeded.connect(lambda output, token=token: self._on_success(output, token))
        self.worker.failed.connect(lambda message, token=token: self._on_failure(message, token))
        self.worker.scope_ready.connect(lambda plan, token=token: self._on_scope_ready(plan, token))
        self.worker.finished.connect(lambda token=token: self._on_worker_finished(token))
        self._set_running(True)
        self._refresh_queue_table()
        self.worker.start()

    def _on_scope_ready(self, plan, token=None) -> None:
        if not self._accept_token(token):
            return
        self._set_running(False)
        item_id = self._current_queue_id
        request = self.worker.request if self.worker is not None else self._pending_collection_request
        if item_id is not None:
            self.queue.finish(item_id, "awaiting_confirm", "等待确认收藏夹范围")
            self._current_queue_id = None
        if request is None:
            self._refresh_queue_table()
            return
        confirm_dlg = ConfirmPlanDialog(plan, self)
        if confirm_dlg.exec() != QDialog.DialogCode.Accepted or not confirm_dlg.confirmed:
            if item_id is not None:
                catalog.update_queue_item(
                    self.database,
                    item_id,
                    status="cancelled",
                    error_message="已取消该项的范围确认",
                )
            self._append_log("已取消这项收藏夹确认，队列继续下一项。")
            self._advance_queue()
            return
        request.confirmed_collection_member_ids = list(plan.member_ids)
        request.conflict_policy = plan.conflict_policy
        request.queue_item_id = None
        if item_id is not None:
            catalog.update_queue_item(
                self.database,
                item_id,
                status="waiting",
                request_json=json.dumps(request.snapshot(), ensure_ascii=False),
                error_message="",
                finished_at=None,
            )
        self._append_log("收藏夹范围已确认，继续这一项。")
        self._advance_queue()

    def start_login(self) -> None:
        if self.login_worker is not None and self.login_worker.isRunning():
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "登录知乎", "请先停止正在进行的搜集，再登录。")
            return
        self.login_button.setEnabled(False)
        self._append_log("已打开登录窗口。请在浏览器里自行完成扫码或验证，程序不会自动处理验证码。")
        self.login_worker = LoginWorker(AuthManager())
        self.login_worker.finished_status.connect(self._on_login_finished)
        self.login_worker.start()

    def show_history(self) -> None:
        HistoryWindow(self.database, self.start_request, self).exec()

    def show_library(self) -> None:
        self.library_page.database = self.database
        self.library_page.settings = self.settings
        self._show_page(3)

    def show_failures(self) -> None:
        FailuresWindow(self.database, self.settings, self).exec()

    def show_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.result_settings()
        new_db = updated.resolved_database_path()
        pending_db_switch = (
            new_db.resolve() != self.database.path.resolve()
            and (catalog.count_open_queue(self.database) or (self.worker is not None and self.worker.isRunning()))
        )
        if pending_db_switch:
            updated.database_path = "" if self.database.path.resolve() == default_database_path().resolve() else str(self.database.path)
        replacement = None
        try:
            if not pending_db_switch and new_db.resolve() != self.database.path.resolve():
                replacement = Database(new_db)
            save_settings(updated)
        except (OSError, sqlite3.Error) as exc:
            if replacement is not None:
                replacement.close()
            QMessageBox.warning(self, "设置未保存", f"无法应用新设置，已保留原配置：{exc}")
            return
        self.settings = updated
        if pending_db_switch:
            QMessageBox.warning(
                self, "设置已保存",
                "队列里还有未结束的项目，数据库仍使用原来的文件。请先完成、继续或删除这些项目，再切换数据库。",
            )
        elif replacement is not None:
            self.database.close()
            self.database = replacement
            self.queue.database = replacement
            self.source_page.database = replacement
            self.library_page.database = replacement
            self._append_log(f"数据库已切换至：{new_db}")
        if self.settings.output_dir:
            self.output_edit.setText(self.settings.output_dir)
        self._append_log("设置已保存。调试模式：" + ("开" if self.settings.headful_debug else "关"))

    def _on_progress(self, progress: Progress) -> None:
        self.status_label.setText(f"状态：{progress.message or progress.status}")
        found_text, done_text = progress_labels(progress)
        self.found_label.setText(found_text)
        self.completed_label.setText(done_text)
        self.current_label.setText(f"当前：{progress.current_title}")
        if progress.percent is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(progress.percent))

    def _on_worker_finished(self, token=None) -> None:
        if not self._accept_token(token, finalize=True):
            return
        self.worker = None
        if getattr(self, "_stopping_worker", False) or getattr(self, "_is_closing", False):
            return
        if self.queue.hold:
            self._refresh_queue_table()
            return
        counts = catalog.queue_counts(self.database)
        if counts.get("needs_login", 0) > 0 or counts.get("needs_verify", 0) > 0 or counts.get("awaiting_confirm", 0) > 0:
            self._refresh_queue_table()
            return
        if counts.get("waiting", 0) > 0:
            self._advance_queue()

    def _on_success(self, output: str, token=None) -> None:
        if not self._accept_token(token):
            return
        self._set_running(False)
        self.progress.setRange(0, 100)
        status = self._finish_current_from_task()
        self._refresh_queue_table()
        kind = status if status in {"failed", "partial", "unconfirmed"} else classify_completion(output)
        if kind == "failed":
            self.status_label.setText("状态：搜集失败")
            title = "搜集失败"
        elif kind == "unconfirmed":
            self.status_label.setText("状态：完整性未确认")
            title = "完整性未确认"
        elif kind == "partial":
            self.status_label.setText("状态：部分完成")
            title = "部分完成"
        else:
            self.status_label.setText("状态：搜集完成")
            title = "搜集完成"
        first_line = output.splitlines()[0] if output else ""
        self._append_log(f"【{title}】{first_line}")

        counts = catalog.queue_counts(self.database)
        has_more = counts.get("waiting", 0) > 0
        if has_more and not self.queue.hold and status not in {"needs_login", "needs_verify", "awaiting_confirm"}:
            self._advance_queue()
            return

        # 弹出包含直接操作按钮的完成摘要对话框
        box = QMessageBox(self)
        box.setWindowTitle("知乎内容搜集器 - " + title)
        box.setText(f"{output}\n\n您可以选择下一步操作：")
        box.setIcon(QMessageBox.Icon.Warning if kind in {"failed", "partial", "unconfirmed"} else QMessageBox.Icon.Information)
        request = self.worker.request if self.worker is not None else None
        library_only = request is not None and request.execution_intent == "library"
        btn_open = None
        if not library_only:
            btn_open = box.addButton("打开输出目录", QMessageBox.ButtonRole.ActionRole)
        btn_lib = box.addButton("查看内容库", QMessageBox.ButtonRole.ActionRole)
        btn_failures = box.addButton("查看失败项", QMessageBox.ButtonRole.ActionRole)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if btn_open is not None and clicked is btn_open:
            self.open_output()
        elif clicked is btn_lib:
            self.show_library()
        elif clicked is btn_failures:
            self.show_failures()
        if status not in {"needs_login", "needs_verify", "awaiting_confirm"} and not self.queue.hold:
            self._advance_queue()

    def _on_failure(self, message: str, token=None) -> None:
        if not self._accept_token(token):
            return
        self._set_running(False)
        self.progress.setRange(0, 100)
        self.status_label.setText("状态：已停止")
        self._append_log(message)
        if message == BUSY_MESSAGE:
            if self._current_queue_id is not None:
                self.queue.release_claim(self._current_queue_id)
                self._current_queue_id = None
            self._refresh_queue_table()
            QMessageBox.warning(self, "知乎内容搜集器", message)
            return
        status = self._finish_current_from_task()
        if self._cancel_current:
            self._cancel_current = False
            if self._last_finished_id is not None:
                catalog.update_queue_item(
                    self.database,
                    self._last_finished_id,
                    status="cancelled",
                    error_message="已取消当前项",
                )
            status = "cancelled"
        self._refresh_queue_table()
        if message.startswith("任务已停止"):
            return
        if status in {"needs_login", "needs_verify"}:
            self.queue.hold = True
            self.queue_notice.setText("联网队列已停下。请处理登录或验证后，再点「继续队列」。")
            QMessageBox.warning(self, "知乎内容搜集器", message)
            return

        counts = catalog.queue_counts(self.database)
        has_more = counts.get("waiting", 0) > 0
        if has_more and not self.queue.hold:
            self._append_log(f"【当前项失败】{message}。队列将自动继续下一项。")
            self._advance_queue()
            return

        QMessageBox.warning(self, "知乎内容搜集器", message)
        if not self.queue.hold:
            self._advance_queue()

    def _on_login_finished(self, status) -> None:
        self.login_button.setEnabled(True)
        self._refresh_login_label()
        self._append_log(status.message)
        QMessageBox.information(self, "登录知乎", status.message)

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _refresh_login_label(self) -> None:
        if AuthManager().has_saved_state():
            self.login_label.setText("登录状态：已保存。搜集时会自动加载。失效后请重新登录。")
        else:
            self.login_label.setText("登录状态：尚未登录。公开页打不开时，请先登录知乎。")

    def _set_running(self, running: bool) -> None:
        self.pause_button.setEnabled(running)
        self.stop_button.setEnabled(running)
        self.cancel_current_button.setEnabled(running)
        self.start_queue_button.setEnabled(not running)
        self.resume_button.setEnabled(True)
        if not running and self._pending_database_path is not None:
            target_db = self._pending_database_path
            self._pending_database_path = None
            try:
                self.database.close()
                self.database = Database(target_db)
                self._append_log(f"后台任务已结束，数据库已切换至：{target_db}")
            except Exception as exc:
                logger.exception("切换延后数据库失败：%s", exc)
                self._append_log(f"切换数据库失败：{exc}")

    def _remember_export_choices(self, request: CollectRequest) -> None:
        updated = replace(
            self.settings,
            export_markdown=request.export_markdown, export_docx=request.export_docx,
            export_pdf=request.export_pdf, export_separate=request.export_separate,
            export_merged=request.export_merged, download_images=request.download_images,
            save_metadata=request.save_metadata, output_dir=request.output_dir,
        )
        try:
            save_settings(updated)
        except OSError as exc:
            self._append_log(f"未能记住本次导出选项，已保留原设置：{exc}")
        else:
            self.settings = updated

    def open_output(self) -> None:
        path = Path(self.output_edit.text()).resolve()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, "打开目录", f"输出目录尚不存在：\n{path}")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._is_closing = True
        self.queue.hold = True
        # Offline export/retry dialogs share this database. Closing the parent
        # must also wait for their workers, even while a modal dialog is open.
        pending_dialog = False
        for dialog in self.findChildren(QDialog):
            worker = getattr(dialog, "worker", None)
            if worker is None or not worker.isRunning():
                continue
            dialog._parent_closing = True
            dialog.close()  # Requests cancellation and retains its running worker.
            pending_dialog = pending_dialog or worker.isRunning()
        if pending_dialog:
            self.status_label.setText("状态：正在安全停止导出或重试任务...")
            event.ignore()
            # Also covers a finished-signal race between inspecting and closing
            # the child. Keep the event loop alive until all workers have ended.
            QTimer.singleShot(100, self.close)
            return
        if self.worker is not None and self.worker.isRunning():
            if not getattr(self, "_stopping_worker", False):
                box = QMessageBox.question(
                    self,
                    "退出程序",
                    "当前有搜集任务正在运行，确定要停止任务并退出吗？\n已保存的内容与文件会保留。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if box != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self._stopping_worker = True
                self.status_label.setText("状态：正在安全停止任务...")
                self.controller.stop()
                try:
                    self.worker.finished.connect(self.close)
                except Exception:
                    pass
            if not self.worker.wait(100):
                event.ignore()
                return

        if self.login_worker is not None and self.login_worker.isRunning():
            self.status_label.setText("状态：正在安全停止登录操作...")
            if not getattr(self, "_stopping_login", False):
                self._stopping_login = True
                try:
                    if hasattr(self.login_worker, "cancel"):
                        self.login_worker.cancel()
                except Exception:
                    pass
                try:
                    self.login_worker.finished.connect(self.close)
                except Exception:
                    pass
            if not self.login_worker.wait(100):
                event.ignore()
                return

        self.database.close()
        event.accept()
