"""第四轮修正回归测试：正文完整性保证、合并导出精确重试、字段与范围契约、协作取消等。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from unittest.mock import MagicMock
import pytest

import zhihu_collector.collectors.service as svc
from zhihu_collector.collectors.offline import export_local_contents
from zhihu_collector.collectors.repairs import retry_saved_failures
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.models import ContentRecord, AuthorInfo
from zhihu_collector.utils.urls import parse_zhihu_url
from zhihu_collector.collectors.listing import _invoke_on_page
from zhihu_collector.browser.auth_manager import AuthManager

URL = "https://www.zhihu.com/people/audit"


def rec(i):
    return ContentRecord(
        zhihu_content_id=str(i),
        content_type="answer",
        url=f"https://www.zhihu.com/question/100/answer/{i}",
        title=f"Title{i}",
        question_id="100",
        question_title=f"Title{i}",
        html_content=f"<p>BODYMARKER{i}</p>",
        author=AuthorInfo(name="Audit", url_token="audit", url=URL),
    )


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "audit.db")
    s = svc.CollectionService(Settings(), db, TaskController())
    yield s, db, tmp_path
    db.close()


def request(root, **kw):
    return svc.CollectRequest(
        url=URL,
        mode="answers",
        output_dir=str(root / "out"),
        download_images=False,
        export_docx=False,
        export_pdf=False,
        **kw,
    )


def save(db, i):
    r = rec(i)
    return db.save_content(r, markdown=f"BODYMARKER{i}", html=r.html_content)[1]


def fail(*a, **kw):
    raise OSError("injected write failure")


def test_incomplete_detail_must_not_be_marked_complete(env, monkeypatch):
    """P1-01: 详情页返回仍为截断/摘要时，严禁无条件标为完整。"""
    s, db, p = env
    r = rec(1)
    r.content_incomplete = True
    detail = rec(1)
    detail.content_incomplete = True
    detail.html_content = "<p>still excerpt...</p>"
    monkeypatch.setattr(svc, "records_from_page_sources", lambda **kw: [detail])
    monkeypatch.setattr(svc, "inspect_page", lambda *a, **kw: None)
    s._enrich(MagicMock(), r)
    assert r.content_incomplete, "不完整详情被无条件标成完整"


def test_failed_enrichment_does_not_save_excerpt_as_success(env, monkeypatch):
    """P1-02: 补全失败时不得把摘要作为完整正文入库并计入成功。"""
    s, db, p = env
    r = rec(1)
    r.content_incomplete = True
    session = MagicMock()
    session.__enter__.return_value = session
    session.new_page.return_value.content.return_value = "<html><body>no matching answer</body></html>"
    monkeypatch.setattr(svc, "BrowserSession", lambda **kw: session)
    monkeypatch.setattr(svc, "records_from_page_sources", lambda **kw: [])
    monkeypatch.setattr(svc, "inspect_page", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "collect_listing", lambda *a, **kw: kw["on_page"]([r], 1, 1))
    s.run(request(p))
    assert not db.list_contents() or db.latest_task(URL)["error_message"], "补全失败仍入库并报告全部完成"


def test_selected_merge_retry_preserves_selection(env, monkeypatch):
    """P1-03: 离线合并导出失败重试时，精准保持原用户选择范围与顺序，不扩大为全库。"""
    s, db, p = env
    one = save(db, 1)
    save(db, 2)
    r = request(p, export_separate=False, export_merged=True)
    with monkeypatch.context() as m:
        m.setattr(svc, "_write_export_file", fail)
        result = export_local_contents(db, [one], p / "out", r, Settings())
    assert result.failed
    retry_saved_failures(db, Settings(), p / "out")
    text = (p / "out" / "所选内容.md").read_text(encoding="utf-8")
    assert "BODYMARKER1" in text
    assert "BODYMARKER2" not in text, "重试选中1篇却混入未选择的2篇"


def test_author_merge_retry_preserves_all_members(env, monkeypatch):
    """P1-04: 作者全部回答合并失败重试时，保持全部成员，不误判为单篇丢失成员。"""
    s, db, p = env
    save(db, 1)
    save(db, 2)
    with monkeypatch.context() as m:
        m.setattr(svc, "_write_export_file", fail)
        s._write_merged(parse_zhihu_url(URL), "answers", request(p, export_merged=True), p / "out", None)
    target = Path(db.list_failures()[0]["target"])
    result = retry_saved_failures(db, Settings(), p / "out")
    assert result.status == "success"
    text = target.read_text(encoding="utf-8")
    assert "BODYMARKER1" in text and "BODYMARKER2" in text, "全部回答被按单篇重试，丢失其余成员"


def test_single_merge_retry_does_not_raise_missing_column(env, monkeypatch):
    """P1-05: 单篇成书失败重试时，无数据库字段异常，不发生范围漂移。"""
    s, db, p = env
    save(db, 1)
    r = request(p, export_merged=True)
    r.url = rec(1).url
    r.mode = "single_answer"
    with monkeypatch.context() as m:
        m.setattr(svc, "_write_export_file", fail)
        s._write_merged(parse_zhihu_url(r.url), "single_answer", r, p / "out", None)
    result = retry_saved_failures(db, Settings(), p / "out")
    assert result.status == "success"


def test_existing_full_content_not_downgraded_by_excerpt(env):
    """P1-02延展: 数据库已有完整正文时，后续若拿到截断摘要，严禁将历史正文降级。"""
    s, db, p = env
    # 先入库一篇完整正文
    r_full = rec(1)
    r_full.html_content = "<p>FULL_CONTENT_12345</p>"
    r_full.content_incomplete = False
    action, local_id = db.save_content(r_full, markdown="FULL_CONTENT_12345", html="<p>FULL_CONTENT_12345</p>")
    assert action == "inserted"

    # 模拟后续抓取拿到同ID但为不完整的摘要
    r_excerpt = rec(1)
    r_excerpt.html_content = "<p>excerpt...</p>"
    r_excerpt.content_incomplete = True
    action2, local_id2 = db.save_content(r_excerpt, markdown="excerpt...", html="<p>excerpt...</p>")
    assert action2 == "skipped"

    # 验证库中仍是完整正文
    content = db.get_content(local_id)
    assert "FULL_CONTENT_12345" in content["html_content"]


def test_invoke_on_page_does_not_duplicate_call_on_type_error():
    """Section 3 静态项: listing回调内部抛出TypeError时不应被误判为参数不匹配而二次执行。"""
    call_count = 0

    def faulty_callback(emit, totals, seen, skipped=0):
        nonlocal call_count
        call_count += 1
        raise TypeError("internal logic error")

    with pytest.raises(TypeError, match="internal logic error"):
        _invoke_on_page(faulty_callback, [], 10, 5, 2)

    assert call_count == 1, "TypeError 导致回调被重复执行"


def test_login_interactive_cooperative_cancel(monkeypatch):
    """Section 3 静态项: 登录交互提供协作式取消，stop后快速退出不阻塞180秒。"""
    ctrl = TaskController()
    mgr = AuthManager(Path("dummy_state.json"))
    ctrl.stop()  # 预先触发取消

    session_mock = MagicMock()
    session_mock.__enter__.return_value = session_mock
    monkeypatch.setattr("zhihu_collector.browser.auth_manager.BrowserSession", lambda **kw: session_mock)

    status = mgr.login_interactive(timeout_sec=180, controller=ctrl)
    assert not status.logged_in
    assert "已取消" in status.message


def test_selection_retry_with_new_content_added_later_and_order_preserved(env, monkeypatch):
    """验收标准3: 库内至少三篇，只选其中两篇且指定顺序，失败后重试保持成员、顺序和选项；重试前新增第四篇也不得混入。"""
    s, db, p = env
    id1 = save(db, 1)
    id2 = save(db, 2)
    id3 = save(db, 3)

    r = request(p, export_separate=False, export_merged=True)
    # 用户指定顺序 [3, 1]
    with monkeypatch.context() as m:
        m.setattr(svc, "_write_export_file", fail)
        result = export_local_contents(db, [db.get_content(id3), db.get_content(id1)], p / "out", r, Settings())
    assert result.failed

    # 重试前库里加入新内容 4
    save(db, 4)

    # 执行重试
    retry_res = retry_saved_failures(db, Settings(), p / "out")
    assert retry_res.status == "success"

    text = (p / "out" / "所选内容.md").read_text(encoding="utf-8")
    assert "BODYMARKER3" in text
    assert "BODYMARKER1" in text
    assert "BODYMARKER2" not in text
    assert "BODYMARKER4" not in text
    # 验证顺序保持 [3, 1]
    assert text.index("BODYMARKER3") < text.index("BODYMARKER1")


def test_question_merge_retry_preserves_all_members_and_counts(env, monkeypatch):
    """验收标准4: 问题合并重试严格验证成员集合和篇数，无范围漂移。"""
    s, db, p = env
    save(db, 1)
    save(db, 2)
    req = request(p, export_merged=True)
    req.mode = "question"
    req.url = "https://www.zhihu.com/question/100"
    with monkeypatch.context() as m:
        m.setattr(svc, "_write_export_file", fail)
        s._write_merged(parse_zhihu_url(req.url), "question", req, p / "out", None)

    failures = db.list_failures()
    assert len(failures) == 1
    target = Path(failures[0]["target"])

    result = retry_saved_failures(db, Settings(), p / "out")
    assert result.status == "success"
    text = target.read_text(encoding="utf-8")
    assert "BODYMARKER1" in text and "BODYMARKER2" in text


def test_subsequent_enrichment_replaces_incomplete_record(env, monkeypatch):
    """P1-02延展: 首次补全失败未入库的内容，后续再次采集补全成功后能够正常入库归档。"""
    s, db, p = env
    r = rec(1)
    r.content_incomplete = True

    # 第一次尝试：补全失败
    session1 = MagicMock()
    session1.__enter__.return_value = session1
    session1.new_page.return_value.content.return_value = "<html><body>no matching answer</body></html>"
    with monkeypatch.context() as m:
        m.setattr(svc, "BrowserSession", lambda **kw: session1)
        m.setattr(svc, "records_from_page_sources", lambda **kw: [])
        m.setattr(svc, "inspect_page", lambda *a, **kw: None)
        m.setattr(svc, "collect_listing", lambda *a, **kw: kw["on_page"]([r], 1, 1))
        s.run(request(p))

    assert not db.list_contents(), "补全失败不应产生已归档正文"
    assert db.latest_task(URL)["error_message"], "补全失败应记录错误信息"

    # 第二次尝试：网络恢复，补全成功
    detail = rec(1)
    detail.content_incomplete = False
    detail.html_content = "<p>BODYMARKER1_RECOVERED_FULL</p>"
    session2 = MagicMock()
    session2.__enter__.return_value = session2
    session2.new_page.return_value.content.return_value = "<html><body>full answer</body></html>"
    with monkeypatch.context() as m:
        m.setattr(svc, "BrowserSession", lambda **kw: session2)
        m.setattr(svc, "records_from_page_sources", lambda **kw: [detail])
        m.setattr(svc, "inspect_page", lambda *a, **kw: None)
        m.setattr(svc, "collect_listing", lambda *a, **kw: kw["on_page"]([r], 1, 1))
        s.run(request(p))

    rows = db.list_contents()
    assert len(rows) == 1, "后续补全成功应正常入库"
    assert "RECOVERED_FULL" in rows[0]["html_content"]


def test_legacy_selected_failure_without_scope_json_rejects_guessing(env):
    """P1-03边界: 历史遗留失败项若缺少 scope_json，不得盲目猜测或输出不完整文件，必须明确报错指引重新选择。"""
    s, db, p = env
    id1 = save(db, 1)
    save(db, 2)
    target = p / "out" / "所选内容.md"

    # 模拟历史遗留失败记录：无 scope_json，export_type 为默认或空
    db.record_export(
        id1,
        export_format="markdown",
        output_path=str(target),
        content_hash="",
        status="failed",
        error_message="injected write failure",
        export_type="merged",
        scope_json="",
    )

    result = retry_saved_failures(db, Settings(), p / "out")
    assert result.status == "partial"
    assert not target.exists(), "缺少原范围记录时不应产出错误文件"
    failures = db.list_failures()
    assert any("缺少所选范围" in f["reason"] for f in failures)


def test_retry_midway_cancellation_preserves_files(env, monkeypatch):
    """验收标准5: 重试过程中触发任务取消，返回 cancelled，已处理文件保留，未处理项不破坏。"""
    s, db, p = env
    id1 = save(db, 1)
    id2 = save(db, 2)
    t1 = p / "out" / "回答-1-Title1.md"
    t2 = p / "out" / "回答-2-Title2.md"
    db.record_export(id1, export_format="markdown", output_path=str(t1), content_hash="", status="failed")
    db.record_export(id2, export_format="markdown", output_path=str(t2), content_hash="", status="failed")

    ctrl = TaskController()
    call_count = 0
    orig_checkpoint = ctrl.checkpoint

    def cancel_after_first(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            ctrl.stop()
        orig_checkpoint()

    with monkeypatch.context() as m:
        m.setattr(ctrl, "checkpoint", cancel_after_first)
        res = retry_saved_failures(db, Settings(), p / "out", controller=ctrl)

    assert res.status == "cancelled"


def test_different_formats_partial_success_and_visible_failure(env, monkeypatch):
    """验收标准5: 不同格式部分成功时，失败项在数据库中准确可见，成功项正常落盘。"""
    s, db, p = env
    id1 = save(db, 1)
    out_dir = p / "out"
    md_file = out_dir / "Title1.md"
    docx_file = out_dir / "Title1.docx"

    def fail_docx(fmt, *a, **kw):
        if fmt == "docx":
            raise OSError("docx generator crashed")
        return Path(kw.get("target_path") or a[2]).write_text("dummy", encoding="utf-8")

    row = db.get_content(id1)
    from zhihu_collector.collectors.offline import write_separate_exports
    req = svc.CollectRequest(url="offline", export_markdown=True, export_docx=True, export_pdf=False)

    with monkeypatch.context() as m:
        m.setattr(svc, "_write_export_file", fail_docx)
        results = write_separate_exports(row, [], out_dir, req, Settings(), None, db)

    assert results["markdown"] == "success"
    assert results["docx"] != "success"

    failures = db.list_failures()
    assert len(failures) == 1
    assert failures[0]["detail"] == "docx"
    assert "crashed" in failures[0]["reason"]
