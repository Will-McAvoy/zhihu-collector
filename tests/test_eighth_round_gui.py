"""第8轮：界面交互与队列行为测试（批量去同步、任务页入口、常用来源操作、内容库任务筛选、非阻塞队列）。"""

import json
import os
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from zhihu_collector.config import Settings
from zhihu_collector.database import catalog
from zhihu_collector.database.database import Database
from zhihu_collector.gui.library_window import LibraryWindow
from zhihu_collector.gui.main_window import MainWindow
from zhihu_collector.gui.source_page import SourcePage


def _make_window(tmp_path, monkeypatch):
    settings = Settings(
        database_path=str(tmp_path / "zhihu.db"),
        output_dir=str(tmp_path / "output"),
    )
    monkeypatch.setattr("zhihu_collector.gui.main_window.load_settings", lambda: settings)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    return app, window


def test_batch_input_desync_invalidation(tmp_path, monkeypatch):
    app, window = _make_window(tmp_path, monkeypatch)
    try:
        # 1. 模拟输入并识别
        window.batch_edit.setPlainText("https://www.zhihu.com/people/test-user")
        window._recognize_batch()
        assert len(window._batch_items) == 1
        assert "1" in window.batch_summary.text()

        # 2. 修改文本后，应自动清除过期的识别结果
        window.batch_edit.setPlainText("https://www.zhihu.com/people/another-user")
        assert len(window._batch_items) == 0
        assert "输入内容已修改" in window.batch_summary.text()
    finally:
        window.close()
        app.processEvents()


def test_save_current_as_source(tmp_path, monkeypatch):
    app, window = _make_window(tmp_path, monkeypatch)
    try:
        warn_msgs = []
        info_msgs = []
        monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warn_msgs.append(text))
        monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: info_msgs.append(text))

        # 1. 空链接点击
        window.url_edit.setText("")
        window._save_current_as_source()
        assert any("输入知乎链接" in m for m in warn_msgs)

        # 2. 有效链接
        window.url_edit.setText("https://www.zhihu.com/people/saved-user")
        window._save_current_as_source()
        assert any("已成功保存为常用来源" in m for m in info_msgs)

        # 3. 检查数据库中是否存在
        sources = catalog.list_sources(window.database)
        assert any("saved-user" in s["canonical_url"] for s in sources)
    finally:
        window.close()
        app.processEvents()


def test_task_page_actions(tmp_path, monkeypatch):
    app, window = _make_window(tmp_path, monkeypatch)
    try:
        # 创建一个仅入库的队列项
        items = catalog.enqueue_requests(
            window.database,
            [{
                "request_json": json.dumps({
                    "url": "https://www.zhihu.com/people/u1",
                    "mode": "answers",
                    "execution_intent": "library",
                }),
                "dedupe_key": "user:u1|answers",
                "source_id": None,
            }],
            batch_id="batch_test",
        )
        q_id = items[0]
        window._refresh_queue_table()
        assert window.queue_table.rowCount() == 1

        # 1. 未完成时点击“查看本次内容”提示尚未执行完成
        info_msgs = []
        monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: info_msgs.append(text))
        window.queue_table.selectRow(0)
        window._view_selected_task_content()
        assert any("尚未执行完成" in m for m in info_msgs)

        # 2. 模拟关联 task_id 并已完成
        task_id = window.database.create_task(
            task_type="answers",
            target_url="https://www.zhihu.com/people/u1",
            target_id="u1",
            output_dir=str(tmp_path / "output_u1"),
            options={"execution_intent": "library"},
        )
        catalog.update_queue_item(window.database, q_id, task_id=task_id, status="success")
        window._refresh_queue_table()
        window.queue_table.selectRow(0)

        # 点击“查看本次内容”切换到内容库并带 task_id 筛选
        window._view_selected_task_content()
        assert window.library_page._task_id == task_id
        assert window.library_page.task_filter_bar.isHidden() is False

        # 3. 仅入库模式点击“打开输出目录”给出说明，不误打开空目录
        info_msgs.clear()
        window._open_selected_task_output()
        assert any("仅更新内容库" in m for m in info_msgs)

        # 4. “查看结果详情”展示详情弹窗
        info_msgs.clear()
        window._view_selected_task_detail()
        assert any("目标链接" in m and "仅更新内容库" in m for m in info_msgs)
    finally:
        window.close()
        app.processEvents()


def test_library_window_task_filter_and_clear(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "zhihu.db"),
        output_dir=str(tmp_path / "output"),
    )
    database = Database(tmp_path / "zhihu.db")
    app = QApplication.instance() or QApplication([])
    lib_win = LibraryWindow(database=database, settings=settings, task_id=123)
    try:
        assert lib_win._task_id == 123
        assert lib_win.task_filter_bar.isHidden() is False
        assert "123" in lib_win.task_filter_label.text()

        # 切换结果类型
        lib_win.task_outcome_combo.setCurrentIndex(1)  # 仅本次新增
        assert lib_win.task_outcome_combo.currentData() == "inserted"

        # 清除任务筛选
        lib_win._clear_task_filter()
        assert lib_win._task_id is None
        assert lib_win.task_filter_bar.isHidden() is True
    finally:
        lib_win.close()
        database.close()
        app.processEvents()


def test_queue_non_blocking_on_success_and_failure(tmp_path, monkeypatch):
    app, window = _make_window(tmp_path, monkeypatch)
    try:
        # 入队 2 个任务
        catalog.enqueue_requests(
            window.database,
            [
                {
                    "request_json": json.dumps({"url": "https://www.zhihu.com/people/u1", "mode": "answers", "execution_intent": "library"}),
                    "dedupe_key": "user:u1|answers",
                    "source_id": None,
                },
                {
                    "request_json": json.dumps({"url": "https://www.zhihu.com/people/u2", "mode": "answers", "execution_intent": "library"}),
                    "dedupe_key": "user:u2|answers",
                    "source_id": None,
                },
            ],
            batch_id="batch_multi",
        )
        window._refresh_queue_table()

        # 取出第一项作为当前执行项
        item1 = window.queue.try_begin()
        assert item1 is not None
        window._current_queue_id = int(item1["id"])

        advance_calls = []
        monkeypatch.setattr(window, "_advance_queue", lambda: advance_calls.append(True))

        # 拦截任何弹窗以防测试挂起
        box_execs = []
        monkeypatch.setattr(QMessageBox, "exec", lambda self: box_execs.append(True))

        # 1. 第一项完成：因为还有等待项，_on_success 应该直接调用 _advance_queue()，不弹出模态阻塞框
        window._on_success("搜集完成：共采集 3 篇")
        assert len(advance_calls) == 1
        assert len(box_execs) == 0

        # 2. 第二项失败：测试普通失败同样非阻塞自动前进
        # 再入队一个第三项保证 has_more 仍然为 True
        catalog.enqueue_requests(
            window.database,
            [{
                "request_json": json.dumps({"url": "https://www.zhihu.com/people/u3", "mode": "answers", "execution_intent": "library"}),
                "dedupe_key": "user:u3|answers",
                "source_id": None,
            }],
            batch_id="batch_multi",
        )
        item2 = window.queue.try_begin()
        assert item2 is not None
        window._current_queue_id = int(item2["id"])

        advance_calls.clear()
        warn_boxes = []
        monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warn_boxes.append(text))

        window._on_failure("网络波动连接重置")
        # 普通网络失败非阻塞调用 _advance_queue
        assert len(advance_calls) == 1
        assert len(warn_boxes) == 0  # 没有弹阻塞警告框

        # 3. 遇到登录失效时：队列必须暂停，不得继续调用 _advance_queue
        item3 = window.queue.try_begin()
        assert item3 is not None
        window._current_queue_id = int(item3["id"])
        # 在数据库中把该任务标记为 needs_login
        task_id = window.database.create_task(
            task_type="answers",
            target_url="https://www.zhihu.com/people/u3",
            target_id="u3",
            output_dir=str(tmp_path / "output_u3"),
            options={},
        )
        window.database.update_task(task_id, status="needs_login", error_message="登录已失效")
        catalog.update_queue_item(window.database, int(item3["id"]), task_id=task_id)

        advance_calls.clear()
        warn_boxes.clear()
        window._on_failure("知乎登录已失效，请重新登录")
        assert len(advance_calls) == 0
        assert window.queue.hold is True
        assert "登录" in window.queue_notice.text()
        assert len(warn_boxes) == 1
    finally:
        window.close()
        app.processEvents()


def test_source_page_sorting_and_moving(tmp_path, monkeypatch):
    settings = Settings(
        database_path=str(tmp_path / "zhihu.db"),
        output_dir=str(tmp_path / "output"),
    )
    database = Database(tmp_path / "zhihu.db")
    from zhihu_collector.presets import PresetManager
    preset_manager = PresetManager(tmp_path / "presets.json")

    s1, _ = catalog.save_source(database, display_name="A来源", canonical_url="https://www.zhihu.com/people/a", target_kind="user", identity_key="user:a", collect_mode="answers")
    s2, _ = catalog.save_source(database, display_name="B来源", canonical_url="https://www.zhihu.com/people/b", target_kind="user", identity_key="user:b", collect_mode="answers")

    app = QApplication.instance() or QApplication([])
    page = SourcePage(database, preset_manager, on_enqueue=lambda reqs: None, on_open_library=lambda src_id: None)
    try:
        assert page.table.rowCount() == 2

        # 切换排序为按名称
        page.sort_combo.setCurrentIndex(2)  # name_asc
        assert page.sort_combo.currentData() == "name_asc"

        # 切换为自定义顺序
        page.sort_combo.setCurrentIndex(1)  # order_asc
        assert page.sort_combo.currentData() == "order_asc"

        # 选中第 2 行执行上移
        page.table.selectRow(1)
        page._move(-1)
        # 刷新后 s2 应排在第一位
        rows = database._conn.execute("SELECT id, sort_order FROM sources ORDER BY sort_order").fetchall()
        assert int(rows[0]["id"]) == s2
        assert int(rows[1]["id"]) == s1
    finally:
        page.close()
        database.close()
        app.processEvents()


def test_source_page_edit_options_mode_restriction_and_integrity(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QComboBox, QDialog, QMessageBox
    database = Database(tmp_path / "zhihu.db")
    from zhihu_collector.presets import PresetManager
    preset_manager = PresetManager(tmp_path / "presets.json")

    s1, _ = catalog.save_source(
        database,
        display_name="知乎问题",
        canonical_url="https://www.zhihu.com/question/123",
        target_kind="question",
        identity_key="question:123",
        collect_mode="question",
    )
    s2, _ = catalog.save_source(
        database,
        display_name="答主回答",
        canonical_url="https://www.zhihu.com/people/author1",
        target_kind="user",
        identity_key="user:author1",
        collect_mode="answers",
    )
    s3, _ = catalog.save_source(
        database,
        display_name="答主想法",
        canonical_url="https://www.zhihu.com/people/author1",
        target_kind="user",
        identity_key="user:author1",
        collect_mode="pins",
    )

    app = QApplication.instance() or QApplication([])
    page = SourcePage(database, preset_manager, on_enqueue=lambda reqs, ids: None, on_open_library=lambda src_id: None)
    try:
        # 1. 验证对于 question 目标，模式被限制为仅 question
        s1_row = next(i for i, r in enumerate(page._rows) if r["id"] == s1)
        page.table.selectRow(s1_row)
        dialog_modes = []

        def mock_exec_question(self_dlg):
            combo = self_dlg.findChildren(QComboBox)[0]
            dialog_modes.extend([combo.itemData(i) for i in range(combo.count())])
            return QDialog.DialogCode.Rejected

        monkeypatch.setattr(QDialog, "exec", mock_exec_question)
        page._edit_options()
        assert dialog_modes == ["question"], f"Expected only ['question'], got {dialog_modes}"

        # 2. 验证唯一键冲突时的友好提示
        s3_row = next(i for i, r in enumerate(page._rows) if r["id"] == s3)
        page.table.selectRow(s3_row)
        warnings = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))

        def mock_exec_conflict(self_dlg):
            combo = self_dlg.findChildren(QComboBox)[0]
            # 尝试切换为已存在的 answers 模式
            idx = combo.findData("answers")
            if idx >= 0:
                combo.setCurrentIndex(idx)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(QDialog, "exec", mock_exec_conflict)
        page._edit_options()
        assert len(warnings) == 1
        assert "无法保存重复项" in warnings[0][2]
    finally:
        page.close()
        database.close()
        app.processEvents()


def test_source_page_export_confirmation_dialog(tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QMessageBox
    database = Database(tmp_path / "zhihu.db")
    from zhihu_collector.presets import PresetManager
    preset_manager = PresetManager(tmp_path / "presets.json")

    s1, _ = catalog.save_source(
        database,
        display_name="答主1",
        canonical_url="https://www.zhihu.com/people/author1",
        target_kind="user",
        identity_key="user:author1",
        collect_mode="answers",
    )
    s2, _ = catalog.save_source(
        database,
        display_name="答主2",
        canonical_url="https://www.zhihu.com/people/author2",
        target_kind="user",
        identity_key="user:author2",
        collect_mode="answers",
    )

    enqueued = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    app = QApplication.instance() or QApplication([])
    page = SourcePage(
        database,
        preset_manager,
        on_enqueue=lambda reqs, ids: enqueued.append((reqs, ids)),
        on_open_library=lambda src_id: None,
    )
    try:
        # 勾选第 1 项
        page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

        # 单项导出：取消确认则不入队
        from zhihu_collector.gui.confirm_plan_dialog import ConfirmPlanDialog
        monkeypatch.setattr(ConfirmPlanDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
        page._enqueue("export")
        assert len(enqueued) == 0

        # 单项导出：确认后入队
        def mock_confirm(self_dlg):
            self_dlg.confirmed = True
            self_dlg.plan.conflict_policy = "new_copy"
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(ConfirmPlanDialog, "exec", mock_confirm)
        page._enqueue("export")
        assert len(enqueued) == 1
        assert enqueued[0][0][0].conflict_policy == "new_copy"

        # 多项导出：勾选两项，弹出批量确认对话框
        page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
        batch_confirmed = []

        def mock_batch_dialog(self_page, chosen, requests):
            batch_confirmed.append(len(requests))
            for req in requests:
                req.conflict_policy = "skip"
            return True

        monkeypatch.setattr(page, "_confirm_batch_export", lambda c, r: mock_batch_dialog(page, c, r))
        page._enqueue("export")
        assert len(enqueued) == 2
        assert batch_confirmed == [2]
        assert enqueued[1][0][0].conflict_policy == "skip"
        assert enqueued[1][0][1].conflict_policy == "skip"
    finally:
        page.close()
        database.close()
        app.processEvents()
