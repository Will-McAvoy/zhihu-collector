import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from zhihu_collector import __version__
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.export_plan import ExportPlan
from zhihu_collector.gui.confirm_plan_dialog import ConfirmPlanDialog
from zhihu_collector.gui.library_window import LibraryWindow
from zhihu_collector.gui.main_window import MainWindow
from zhihu_collector.gui.presets_dialog import PresetsDialog
from zhihu_collector.presets import PresetManager


def test_main_window_builds(monkeypatch, tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "zhihu.db"),
        output_dir=str(tmp_path / "output"),
    )
    monkeypatch.setattr("zhihu_collector.gui.main_window.load_settings", lambda: settings)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window.windowTitle() == f"知乎内容搜集器 v{__version__}"
    assert window.start_button.text() == "开始搜集"
    assert window.pause_button.isEnabled() is False
    window.close()
    app.processEvents()


def test_main_window_presets_and_collection_url_detection(monkeypatch, tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "zhihu.db"),
        output_dir=str(tmp_path / "output"),
    )
    monkeypatch.setattr("zhihu_collector.gui.main_window.load_settings", lambda: settings)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()

    # 1. 验证预设下拉框已加载
    assert window.preset_combo.count() >= 3
    assert window.preset_status_label.text() == ""

    # 2. 修改选项标记未保存
    window.markdown_box.toggle()
    assert window.preset_status_label.text() == "[已修改，尚未保存]"

    # 3. 粘贴收藏夹链接自动识别并改变主按钮文字
    window.url_edit.setText("https://www.zhihu.com/collection/556677")
    assert window.start_button.text() == "读取收藏夹清单并开始搜集"
    assert window._selected_mode() == "collection"

    # 4. 粘贴回答链接自动切换为仅此回答
    window.url_edit.setText("https://www.zhihu.com/question/100/answer/200")
    assert window.start_button.text() == "开始搜集"
    assert window._selected_mode() == "single_answer"

    window.close()
    app.processEvents()


def test_presets_dialog_ui(tmp_path):
    app = QApplication.instance() or QApplication([])
    mgr = PresetManager(tmp_path / "presets.json")
    dlg = PresetsDialog(mgr)
    assert dlg.windowTitle() == "管理采集配置"
    assert dlg.list_widget.count() == len(mgr.presets)
    dlg.close()
    app.processEvents()


def test_confirm_plan_dialog_ui(tmp_path):
    app = QApplication.instance() or QApplication([])

    # 计划模式
    plan_online = ExportPlan(
        scope_kind="network_plan",
        source_url="https://www.zhihu.com/collection/123",
        source_title="测试计划",
        max_items=20,
        output_root=str(tmp_path),
    )
    dlg_plan = ConfirmPlanDialog(plan_online)
    assert dlg_plan.windowTitle() == "确认采集计划"
    assert "最多采集 20 篇" in dlg_plan.scope_label.text()
    dlg_plan.close()

    # 精确模式
    plan_exact = ExportPlan(
        scope_kind="offline_exact",
        source_title="离线导出",
        member_ids=["1", "2"],
        output_root=str(tmp_path),
        member_items=[{"title": "回答1", "author": "作者1", "type": "answer"}],
    )
    dlg_exact = ConfirmPlanDialog(plan_exact)
    assert dlg_exact.windowTitle() == "确认导出范围与选项"
    assert dlg_exact.table is not None
    dlg_exact.close()
    app.processEvents()


def test_library_window_rich_preview_and_search(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(str(tmp_path / "lib.db"))
    settings = Settings(database_path=str(tmp_path / "lib.db"))

    win = LibraryWindow(db, settings)
    assert hasattr(win, "splitter")
    assert hasattr(win, "preview_browser")
    assert hasattr(win, "search_input")
    assert win.select_page_btn.text() == "选择本页"
    assert win.clear_sel_btn.text() == "清空勾选"

    # 测试缩放调用
    initial_font_size = win.preview_browser.font().pointSize()
    win._zoom_in()
    win._zoom_out()
    win._zoom_reset()

    win.close()
    db.close()
    app.processEvents()
