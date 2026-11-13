"""第三轮升级回归测试：涵盖数据完整性、折叠摘要、单篇入口、精确ID匹配、计数与限量、安全退出、合并导出失败持久化及重试、PDF元数据展示等。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from unittest.mock import MagicMock
import pytest

import zhihu_collector.collectors.service as svc
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.models import ContentRecord, AuthorInfo
from zhihu_collector.utils.urls import parse_zhihu_url
from zhihu_collector.exporters.pdf_exporter import markdown_to_html
from zhihu_collector.parsers.dom_parser import parse_answer_cards_html

URL = "https://www.zhihu.com/people/audit"


def record(i="2"):
    return ContentRecord(
        zhihu_content_id=i,
        content_type="answer",
        url=f"https://www.zhihu.com/question/1/answer/{i}",
        title=f"样本{i}",
        question_id="1",
        question_title=f"样本{i}",
        author=AuthorInfo(name="验收", url_token="audit", url=URL),
        html_content=f"<p>正文{i}</p>",
    )


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "audit.db")
    s = svc.CollectionService(Settings(), db, TaskController())
    yield s, db, tmp_path
    db.close()


def req(root, **kwargs):
    return svc.CollectRequest(
        url=URL,
        mode="answers",
        output_dir=str(root / "out"),
        download_images=False,
        export_docx=False,
        export_pdf=False,
        **kwargs,
    )


def fake_listing(monkeypatch, records):
    monkeypatch.setattr(svc, "BrowserSession", MagicMock())
    monkeypatch.setattr(
        svc,
        "collect_listing",
        lambda *a, **kw: kw["on_page"](records, len(records), len(records)),
    )


def test_single_answer_runs_through_real_service_entry(env, monkeypatch):
    s, db, p = env
    monkeypatch.setattr(svc, "BrowserSession", MagicMock())
    reached = []
    monkeypatch.setattr(s, "_collect_single_answer", lambda *a: reached.append(True))
    r = req(p)
    r.url = record().url
    r.mode = "auto"
    s.run(r)
    assert reached


def test_single_answer_bare_url_resolves(env, monkeypatch):
    s, db, p = env
    monkeypatch.setattr(svc, "BrowserSession", MagicMock())
    reached = []
    monkeypatch.setattr(s, "_collect_single_answer", lambda *a: reached.append(True))
    r = req(p)
    r.url = "https://www.zhihu.com/answer/2"
    r.mode = "auto"
    s.run(r)
    assert reached


def test_inserted_record_counts_as_new(env):
    s, db, p = env
    s._persist_record(record(), p / "out", req(p), None, None, index_scope="author")
    assert db.count_contents() == 1
    assert s._new_saved == 1, f"new={s._new_saved}, skipped={s._skipped}"


def test_limit_updates_actual_task_progress(env, monkeypatch):
    s, db, p = env
    fake_listing(monkeypatch, [record(str(i)) for i in range(2, 7)])
    s.run(req(p, max_items=2))
    task = db.latest_task(URL)
    assert db.count_contents() == 2
    assert task["total_completed"] == 2, dict(task)


def test_limit_does_not_hide_export_failure(env, monkeypatch):
    s, db, p = env
    fake_listing(monkeypatch, [record()])

    def failure(*args):
        s._failures += 1
        s._failed_exports += 1

    monkeypatch.setattr(s, "_write_merged", failure)
    s.run(req(p, max_items=1, export_merged=True))
    assert db.latest_task(URL)["error_message"], s.progress.message


def test_single_answer_never_saves_unrelated_answer(env, monkeypatch):
    s, db, p = env
    s.task_id = db.create_task(
        task_type="single_answer",
        target_url=record().url,
        target_id="2",
        output_dir=str(p),
        options={},
    )
    session = MagicMock()
    session.new_page.return_value.url = record().url
    monkeypatch.setattr(svc, "records_from_page_sources", lambda **kw: [record("999")])
    monkeypatch.setattr(svc, "inspect_page", lambda *a, **kw: None)
    r = req(p)
    r.url = record().url
    r.mode = "auto"
    try:
        s._collect_single_answer(session, parse_zhihu_url(r.url), r, p / "out", None, None)
    except Exception:
        pass
    assert not db.list_contents(), "目标回答缺失时不应保存其他回答"


def test_merged_failure_is_visible_in_failure_list(env, monkeypatch):
    s, db, p = env
    s._persist_record(record(), p / "out", req(p, export_separate=False), None, None, index_scope="author")

    def fail(*a, **kw):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(svc, "_write_export_file", fail)
    s._write_merged(parse_zhihu_url(URL), "answers", req(p, export_merged=True), p / "out", None)
    assert s._failed_exports == 1
    failures = db.list_failures()
    assert failures, "合并失败没有可重试的持久记录"
    assert failures[0]["kind"] == "export"
    assert "全部回答" in failures[0]["target"]


def test_export_errors_do_not_allow_limit_overrun(env, monkeypatch):
    s, db, p = env
    fake_listing(monkeypatch, [record(str(i)) for i in range(2, 7)])

    def fail(*a, **kw):
        raise OSError("模拟文件被占用")

    monkeypatch.setattr(svc, "_write_export_file", fail)
    s.run(req(p, max_items=2))
    assert db.count_contents() <= 2, f"limit=2 actual={db.count_contents()}"


def test_gui_close_does_not_force_terminate_worker(env, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtGui import QCloseEvent
    import zhihu_collector.gui.main_window as gui

    app = QApplication.instance() or QApplication([])
    s, db, p = env
    monkeypatch.setattr(gui, "load_settings", lambda: Settings(database_path=str(p / "gui.db")))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    w = gui.MainWindow()
    worker = MagicMock()
    worker.isRunning.return_value = True
    worker.wait.return_value = False
    w.worker = worker
    ev = QCloseEvent()
    w.closeEvent(ev)
    called = worker.terminate.called
    w.worker = None
    assert not called, "正常网络等待超过4秒就调用QThread.terminate"


def test_login_running_blocks_window_destruction(env, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QCloseEvent
    import zhihu_collector.gui.main_window as gui

    app = QApplication.instance() or QApplication([])
    s, db, p = env
    monkeypatch.setattr(gui, "load_settings", lambda: Settings(database_path=str(p / "login-gui.db")))
    w = gui.MainWindow()
    worker = MagicMock()
    worker.isRunning.return_value = True
    worker.wait.return_value = False
    w.login_worker = worker
    ev = QCloseEvent()
    w.closeEvent(ev)
    w.login_worker = None
    assert not ev.isAccepted(), "登录线程仍运行时窗口接受关闭"


def test_collapsed_dom_excerpt_requires_detail_fetch():
    snippet = """<div class="List-item"><h2><a href="/question/1/answer/2">样本</a></h2><div class="RichContent is-collapsed"><div class="RichText">只是一小段摘要…</div><button class="ContentItem-more">阅读全文</button></div></div>"""
    records = parse_answer_cards_html(snippet)
    assert records and records[0].content_incomplete, "折叠摘要被当作完整正文，_enrich会直接跳过"


def test_pdf_frontmatter_stripped_and_formatted(tmp_path):
    md_with_frontmatter = """---
type: answer
author: "测试作者"
question: "测试问题"
url: https://www.zhihu.com/question/1/answer/2
created_at: 2026-01-01T00:00:00
updated_at: 2026-01-02T00:00:00
vote_count: 42
comment_count: 5
---

# 测试标题

这是正文内容。
"""
    html_out = markdown_to_html(md_with_frontmatter, title="测试标题", base_dir=tmp_path)
    assert "type: answer" not in html_out
    assert "vote_count: 42" not in html_out
    assert "发布时间：2026-01-01T00:00:00" in html_out
    assert "赞同数：42" in html_out
    assert "测试标题" in html_out
    assert "max-height: 650px;" in html_out
    assert "object-fit: contain;" in html_out


def test_skipped_unchanged_records_tracked_in_paging_and_service(tmp_path):
    from zhihu_collector.collectors.paging import ScrollState
    from zhihu_collector.collectors.listing import select_records_to_emit, _version
    from zhihu_collector.models import ContentRecord, AuthorInfo

    state = ScrollState()
    rec1 = ContentRecord(zhihu_content_id="101", content_type="answer", url="http://test/101", title="已存1", author=AuthorInfo(name="author"), html_content="<p>正文1</p>")
    rec2 = ContentRecord(zhihu_content_id="102", content_type="answer", url="http://test/102", title="已存2", author=AuthorInfo(name="author"), html_content="<p>正文2</p>")
    known = {"101": _version(rec1), "102": _version(rec2)}

    emitted = select_records_to_emit([rec1, rec2], state=state, known_versions=known)
    assert emitted == []
    assert len(state.skipped_ids) == 2
    assert state.last_skipped_count == 2


def test_retry_saved_failures_handles_multi_directory_and_merged(tmp_path):
    from zhihu_collector.collectors.repairs import retry_saved_failures
    from zhihu_collector.models import ContentRecord, AuthorInfo

    db = Database(tmp_path / "repairs_test.db")
    rec = ContentRecord(
        zhihu_content_id="201",
        content_type="answer",
        url="http://test/201",
        title="测试标题201",
        author=AuthorInfo(name="测试作者", url_token="test_author", url="https://www.zhihu.com/people/test_author"),
        html_content="<p>正文内容201</p>",
    )
    action, local_id = db.save_content(rec, markdown="正文内容201", html="<p>正文内容201</p>")
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    # 模拟在 dir_a 和 dir_b 导出均失败
    target_a = dir_a / "201.md"
    target_b = dir_b / "201.md"
    db.record_export(local_id, export_format="markdown", output_path=str(target_a), content_hash="", status="failed", error_message="A failed")
    db.record_export(local_id, export_format="markdown", output_path=str(target_b), content_hash="", status="failed", error_message="B failed")

    # 模拟合并导出失败
    target_merged = tmp_path / "所选内容.md"
    db.record_export(local_id, export_format="markdown", output_path=str(target_merged), content_hash="", status="failed", error_message="Merged failed")

    failures_before = db.list_failures()
    assert len(failures_before) == 3

    settings = Settings(database_path=str(tmp_path / "repairs_test.db"))
    result = retry_saved_failures(db, settings, tmp_path)
    assert result.status == "success"

    # 检查两个目录及合并文件均已生成
    assert target_a.is_file()
    assert target_b.is_file()
    assert target_merged.is_file()

    # 失败列表应当清空
    failures_after = db.list_failures()
    assert len(failures_after) == 0


def test_subdialogs_close_event_safe_cancellation():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QCloseEvent
    from unittest.mock import MagicMock
    from zhihu_collector.gui.failures_window import FailuresWindow
    from zhihu_collector.gui.library_window import OfflineExportDialog

    app = QApplication.instance() or QApplication([])
    mock_db = MagicMock()
    mock_db.list_failures.return_value = []
    settings = Settings()

    fw = FailuresWindow(mock_db, settings)
    mock_worker = MagicMock()
    mock_worker.isRunning.return_value = True
    mock_worker.wait.return_value = False
    fw.worker = mock_worker
    ev = QCloseEvent()
    fw.closeEvent(ev)
    assert not ev.isAccepted(), "FailuresWindow 在后台重试仍在运行时接受了关闭"

    oed = OfflineExportDialog(mock_db, settings, [1])
    oed.worker = mock_worker
    ev2 = QCloseEvent()
    oed.closeEvent(ev2)
    assert not ev2.isAccepted(), "OfflineExportDialog 在后台导出仍在运行时接受了关闭"
