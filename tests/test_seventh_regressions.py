"""第7轮修正的正式回归：清单遍历、正文、图片、状态、重试和界面。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import zhihu_collector.config as config
import zhihu_collector.collectors.collection_answers as coll
import zhihu_collector.collectors.service as svc
import zhihu_collector.gui.main_window as main_gui
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.offline import export_local_contents, offline_plan_counts
from zhihu_collector.collectors.repairs import retry_body_failures
from zhihu_collector.database.database import Database
from zhihu_collector.errors import CollectionScopeReady, TaskStopped
from zhihu_collector.export_plan import ExportPlan, expected_export_file_count
from zhihu_collector.gui.preview_renderer import render_article_preview
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.parsers.collection_parser import (
    collection_dom_end_state,
    parse_collection_items_detailed,
    payload_matches_collection,
)
from zhihu_collector.presets import Preset

URL = "https://www.zhihu.com/collection/12"


def _record(i="1", incomplete=False, html="", updated=None):
    body = html or ("<p>" + ("EXCERPT" if incomplete else f"FULL BODY {i} END") + "</p>")
    record = ContentRecord(
        zhihu_content_id=str(i),
        content_type="answer",
        url=f"https://www.zhihu.com/question/100/answer/{i}",
        title=f"Article{i}",
        author=AuthorInfo(name="Author", url_token="author"),
        html_content=body,
        content_incomplete=incomplete,
        updated_time=updated,
    )
    return record


def _request(path: Path, **kwargs):
    return svc.CollectRequest(
        url=URL,
        mode="collection",
        output_dir=str(path / "out"),
        download_images=False,
        export_docx=False,
        export_pdf=False,
        conflict_policy="overwrite",
        **kwargs,
    )


def _fast_settings():
    return config.Settings(page_interval_ms=0, request_interval_ms=0, max_stale_rounds=1)


def _item(collection_id, answers, *, is_end=True, nxt="", status=200, url=None):
    data_rows = []
    for answer in answers:
        if isinstance(answer, str):
            kind, ident = answer.split(":", 1)
            data_rows.append({"content": {"type": kind, "id": ident}})
            continue
        data_rows.append({
            "content": {
                "type": "answer",
                "id": int(answer.zhihu_content_id),
                "question": {"id": 100, "title": answer.title},
                "author": {"name": answer.author.name, "url_token": answer.author.url_token},
                "content": answer.html_content,
                "updated_time": answer.updated_time,
            }
        })
    return SimpleNamespace(
        status=status,
        url=url or f"https://www.zhihu.com/api/v4/collections/{collection_id}/items",
        data={"collection": {"id": int(collection_id), "title": "Audit"}, "data": data_rows, "paging": {"is_end": is_end, "next": nxt}},
    )


def _session(pages, html="<html><h1>Audit</h1></html>", errors=None):
    listener = MagicMock()
    listener.snapshot.return_value = pages[0] if pages else []
    queued = [list(page) for page in pages]

    def consume():
        return queued.pop(0) if queued else []

    listener.consume.side_effect = consume
    listener.last_paging = {"is_end": True, "next": ""}
    listener.consume_errors.return_value = list(errors or [])
    session = MagicMock()
    page = session.new_page.return_value
    page.content.return_value = html
    page.url = URL
    page.locator.return_value.count.return_value = 0
    return session, page, listener


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "user_data_root", lambda: tmp_path / "runtime")
    database = Database(tmp_path / "audit.db")
    session = MagicMock()
    session.__enter__.return_value = session
    session.new_page.return_value.content.return_value = "<html><body>No matching answer</body></html>"
    session.new_page.return_value.url = URL
    monkeypatch.setattr(svc, "BrowserSession", lambda **kwargs: session)
    yield database, tmp_path, session
    database.close()


def _manifest(monkeypatch, records, status="complete"):
    manifest = coll.CollectionManifest(
        "12",
        "Audit Collection",
        URL,
        answers=records,
        integrity_status=status,
        finish_reason="end" if status == "complete" else "stalled",
    )
    monkeypatch.setattr(coll, "collect_collection_manifest", lambda *args, **kwargs: manifest)
    return manifest


def test_manifest_one_page_multipage_empty_and_isolated_lists(monkeypatch):
    monkeypatch.setattr(coll, "inspect_page", lambda *args, **kwargs: None)
    monkeypatch.setattr(coll, "NetworkListener", lambda: None)
    settings = _fast_settings()

    def run(pages):
        session, page, listener = _session(pages)
        monkeypatch.setattr(coll, "NetworkListener", lambda: listener)
        return coll.collect_collection_manifest(session, "12", settings, TaskController()), page

    one, page_one = run([[ _item("12", [_record("1")]) ]])
    assert [item.zhihu_content_id for item in one.answers] == ["1"]
    assert one.integrity_status == "complete"
    assert page_one.close.called

    multi, _page = run([
        [_item("12", [_record("1")], is_end=False, nxt="https://www.zhihu.com/api/v4/collections/12/items?offset=1")],
        [_item("12", [_record("2")], is_end=False, nxt="https://www.zhihu.com/api/v4/collections/12/items?offset=2")],
        [_item("12", [_record("3")], is_end=True, nxt="")],
    ])
    assert [item.zhihu_content_id for item in multi.answers] == ["1", "2", "3"]
    assert multi.integrity_status == "complete"
    assert multi.page_summaries is not one.page_summaries
    one.page_summaries.append({"poison": True})
    assert all("poison" not in item for item in multi.page_summaries)

    empty, _page = run([[_item("12", [], is_end=True)]])
    assert empty.answers == []
    assert empty.integrity_status == "empty"


def test_manifest_cancel_and_error_close_page(monkeypatch):
    monkeypatch.setattr(coll, "inspect_page", lambda *args, **kwargs: None)
    settings = _fast_settings()
    session, page, listener = _session([[_item("12", [_record("1")])]])
    monkeypatch.setattr(coll, "NetworkListener", lambda: listener)
    controller = TaskController()
    controller.stop()
    stopped = coll.collect_collection_manifest(session, "12", settings, controller)
    assert stopped.interrupted == "stopped"
    assert stopped.integrity_status != "complete"
    assert page.close.called

    session, page, listener = _session([[]])
    page.goto.side_effect = RuntimeError("boom")
    monkeypatch.setattr(coll, "NetworkListener", lambda: listener)
    with pytest.raises(RuntimeError):
        coll.collect_collection_manifest(session, "12", settings, TaskController())
    assert page.close.called


def test_collection_identity_conflict_domain_and_metadata():
    assert payload_matches_collection({"data": []}, "https://www.zhihu.com/api/v4/questions/12/answers", "12") is False
    assert payload_matches_collection({"collection": {"id": 12}}, "https://www.zhihu.com/api/v4/collections/123/items", "12") is False
    assert payload_matches_collection({"collection": {"id": 123}}, "https://www.zhihu.com/api/v4/collections/12/items", "12") is False
    assert payload_matches_collection({"type": "collection", "id": 12, "title": "元数据"}, "https://www.zhihu.com/api/v4/collections/12", "12") is True
    assert payload_matches_collection({"collection": {"id": 12}}, "https://evil.example/api/v4/collections/12/items", "12") is False
    mixed = {"collection": {"id": 12}, "data": [{"content": {"type": "article", "id": 7}}, {"content": {"type": "answer", "id": 8, "content": "<p>好</p>", "question": {"id": 1, "title": "短"}}}]}
    parsed = parse_collection_items_detailed([mixed])
    assert [item.zhihu_content_id for item in parsed.answers] == ["8"]
    assert parsed.non_answer_count == 1
    assert parsed.answers[0].content_incomplete is False


def test_foreign_paging_duplicate_cursor_and_network_failure(monkeypatch):
    monkeypatch.setattr(coll, "inspect_page", lambda *args, **kwargs: None)
    settings = _fast_settings()
    foreign = _item("12", [_record("1")], is_end=False, nxt="next")
    session, _page, listener = _session([[foreign], []])
    listener.last_paging = {"is_end": True, "next": ""}
    monkeypatch.setattr(coll, "NetworkListener", lambda: listener)
    result = coll.collect_collection_manifest(session, "12", settings, TaskController())
    assert result.integrity_status != "complete"

    cursor = "https://www.zhihu.com/api/v4/collections/12/items?offset=1"
    session, _page, listener = _session([
        [_item("12", [_record("1")], is_end=False, nxt=cursor)],
        [_item("12", [_record("1")], is_end=False, nxt=cursor)],
    ])
    monkeypatch.setattr(coll, "NetworkListener", lambda: listener)
    repeated = coll.collect_collection_manifest(session, "12", settings, TaskController())
    assert repeated.integrity_status == "partial"
    assert repeated.duplicate_count == 1

    failed = _item("12", [], status=500, url="https://www.zhihu.com/api/v4/collections/12/items")
    session, _page, listener = _session([[failed]], errors=[{"url": "https://www.zhihu.com/api/v4/collections/12/items", "status": 500, "message": "fail"}])
    monkeypatch.setattr(coll, "NetworkListener", lambda: listener)
    broken = coll.collect_collection_manifest(session, "12", settings, TaskController())
    assert broken.integrity_status != "complete"
    assert broken.integrity_status != "empty"


def test_dom_ignores_citation_and_recommendation_and_repeat_counts():
    html = """
    <div class="CollectionDetailPage-main">
      <div class="List-item">
        <h2><a href="/question/9/answer/1">成员</a></h2>
        <div class="RichText"><a href="/question/9/answer/999">引用回答 999</a></div>
      </div>
      <div class="Recommendations">
        <div class="List-item"><h2><a href="/question/8/answer/888">推荐</a></h2></div>
      </div>
      <p>暂无内容</p>
    </div>
    """
    parsed = parse_collection_items_detailed([], html, target_collection_id="12")
    assert [item.zhihu_content_id for item in parsed.answers] == ["1"]
    again = parse_collection_items_detailed([], html, target_collection_id="12")
    assert again.non_answer_count == parsed.non_answer_count
    assert again.unknown_type_count == parsed.unknown_type_count
    assert collection_dom_end_state("<html><body>暂无内容</body></html>") == "unknown"


def test_incomplete_excerpt_is_not_saved_and_old_body_is_not_downgraded(env, monkeypatch):
    database, path, _session = env
    _manifest(monkeypatch, [_record(incomplete=True)])
    service = svc.CollectionService(config.Settings(), database, TaskController())
    service.run(_request(path))
    assert database.list_contents() == []
    assert any(item["kind"] == "body" for item in database.list_failures())

    old = _record("9", html="<p>OLD COMPLETE BODY</p>", updated=1577836800)
    database.save_content(old, markdown="OLD COMPLETE BODY", html=old.html_content)
    newer = _record("9", html="<p>NEW COMPLETE BODY END</p>", updated=1700000000)
    _manifest(monkeypatch, [newer])
    svc.CollectionService(config.Settings(), database, TaskController()).run(_request(path))
    stored = database.list_contents(zhihu_content_id="9", content_type="answer")[0]
    assert "NEW COMPLETE BODY END" in stored["html_content"]

    kept = _record("8", html="<p>KEEP COMPLETE BODY</p>", updated=1577836800)
    database.save_content(kept, markdown="KEEP COMPLETE BODY", html=kept.html_content)
    excerpt = _record("8", incomplete=True, html="<p>EXCERPT ONLY</p>", updated=1700000000)
    _manifest(monkeypatch, [excerpt])
    svc.CollectionService(config.Settings(), database, TaskController()).run(_request(path))
    stored = database.list_contents(zhihu_content_id="8", content_type="answer")[0]
    assert "KEEP COMPLETE BODY" in stored["html_content"]
    assert "EXCERPT ONLY" not in stored["html_content"]
    assert any(item["kind"] == "body" and item["zhihu_content_id"] == "8" for item in database.list_failures())


def test_short_and_image_answers_pass_and_images_are_registered(env, monkeypatch, tmp_path):
    from zhihu_collector.downloader.image_downloader import ImageAsset

    database, path, _session = env
    short = _record("1", html="<p>好</p>")
    image_html = '<p>图</p><img src="https://example.org/pic.jpg">'
    pictured = _record("2", html=image_html)
    pictured.image_urls = ["https://example.org/pic.jpg"]
    _manifest(monkeypatch, [short, pictured])
    local = tmp_path / "pic.jpg"
    local.write_bytes(b"image-bytes")
    downloader = MagicMock()
    downloader.download_all.return_value = [ImageAsset(original_url=pictured.image_urls[0], local_path=str(local), download_status="downloaded", sort_index=1)]
    monkeypatch.setattr(svc, "ImageDownloader", lambda **kwargs: downloader)
    request = _request(path)
    request.download_images = True
    svc.CollectionService(config.Settings(), database, TaskController()).run(request)
    rows = {row["zhihu_content_id"]: row for row in database.list_contents()}
    assert "好" in rows["1"]["html_content"]
    images = database.list_images(rows["2"]["id"])
    assert images and images[0]["download_status"] == "downloaded"
    assert database.latest_task(URL)["error_message"] == ""


def test_failed_partial_and_missing_image_are_counted(env, monkeypatch, tmp_path):
    from zhihu_collector.downloader.image_downloader import ImageAsset

    database, path, _session = env
    record = _record("3")
    record.image_urls = ["https://example.org/ok.jpg", "https://example.org/bad.jpg"]
    _manifest(monkeypatch, [record])
    ok = tmp_path / "ok.jpg"
    ok.write_bytes(b"ok")
    downloader = MagicMock()
    downloader.download_all.return_value = [
        ImageAsset(original_url=record.image_urls[0], local_path=str(ok), download_status="downloaded", sort_index=1),
        ImageAsset(original_url=record.image_urls[1], local_path=str(tmp_path / "missing.jpg"), download_status="downloaded", sort_index=2),
    ]
    monkeypatch.setattr(svc, "ImageDownloader", lambda **kwargs: downloader)
    request = _request(path)
    request.download_images = True
    svc.CollectionService(config.Settings(), database, TaskController()).run(request)
    task = database.latest_task(URL)
    assert task["status"] != "completed" or task["error_message"]
    assert task["error_message"]
    stored = database.list_images(database.list_contents()[0]["id"])
    assert len(stored) == 2
    assert any(item["kind"] == "image" for item in database.list_failures())


def test_reused_body_keeps_existing_images(env, monkeypatch):
    database, path, _session = env
    record = _record("4", html="<p>KEEP COMPLETE BODY</p>")
    _action, content_id = database.save_content(record, markdown="KEEP COMPLETE BODY", html=record.html_content)
    image = path / "old.png"
    image.write_bytes(b"png")
    database.replace_images(content_id, [{
        "original_url": "https://example.org/old.png",
        "local_path": str(image),
        "download_status": "downloaded",
        "hash": "abc",
        "sort_index": 1,
    }])
    incoming = _record("4", incomplete=True, html="<p>EXCERPT</p>")
    incoming.image_urls = []
    _manifest(monkeypatch, [incoming])
    svc.CollectionService(config.Settings(), database, TaskController()).run(_request(path))
    assert database.list_images(content_id)


def test_partial_limit_and_report_counts_agree(env, monkeypatch):
    database, path, _session = env
    _manifest(monkeypatch, [_record("1")], status="partial")
    service = svc.CollectionService(config.Settings(), database, TaskController())
    service.run(_request(path))
    task = database.latest_task(URL)
    assert task["status"] != "completed" or task["error_message"]
    assert "搜集完成" not in (service.progress.message or "")
    report = next((path / "out").rglob("导出结果.md"))
    text = report.read_text(encoding="utf-8")
    assert "确认选中回答**：1" in text
    assert "终态合计**：1" in text


def test_retry_restores_only_failed_member_and_keeps_merge_order(env, monkeypatch, tmp_path):
    database, path, _session = env
    first, second = _record("1"), _record("2")
    _manifest(monkeypatch, [first, second])
    options = _request(path, confirmed_collection_member_ids=["1", "2"], export_merged=True, export_separate=True).as_options()
    database.record_content_failure(second, None, options, "only 2 failed")
    database.save_content(first, markdown="FULL BODY 1 END", html=first.html_content)
    merged = path / "out" / "收藏夹"
    retry_body_failures(database, config.Settings())
    assert [row["zhihu_content_id"] for row in database.list_contents()] == ["2", "1"] or set(
        row["zhihu_content_id"] for row in database.list_contents()
    ) == {"1", "2"}
    ids = [row["zhihu_content_id"] for row in database.list_contents()]
    assert ids.count("1") == 1 and ids.count("2") == 1
    assert not [item for item in database.list_failures() if item["kind"] == "body"]
    merged_files = list(Path(path / "out").rglob("*.md"))
    merged_text = "\n".join(file.read_text(encoding="utf-8") for file in merged_files)
    assert merged_text.index("FULL BODY 1 END") < merged_text.index("FULL BODY 2 END")
    separate_for_first = [file for file in merged_files if "Article1" in file.name or "0001" in file.name]
    before = {file: file.read_bytes() for file in separate_for_first}
    retry_body_failures(database, config.Settings())
    for file, payload in before.items():
        assert file.read_bytes() == payload


def test_missing_member_preserves_merge_bytes_and_skip_is_not_new_success(env):
    database, path, _session = env
    record = _record()
    _action, content_id = database.save_content(record, markdown="BODY", html=record.html_content)
    output = path / "out"
    output.mkdir()
    target = output / "所选内容.md"
    target.write_text("OLD SAFE", encoding="utf-8")
    before = target.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    result = export_local_contents(
        database,
        [content_id, 999999],
        output,
        _request(path, export_separate=False, export_merged=True),
        config.Settings(),
    )
    assert target.read_bytes() == before
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    assert result.status != "success"

    request = _request(path, export_separate=False, export_merged=True)
    request.conflict_policy = "skip"
    skipped = export_local_contents(database, [content_id], output, request, config.Settings())
    assert "跳过" in skipped.message and skipped.completed == 0
    assert target.read_text(encoding="utf-8") == "OLD SAFE"


def test_plan_counts_follow_actual_targets_and_output_directory(env):
    database, path, _session = env
    record = _record()
    _action, content_id = database.save_content(record, markdown="BODY", html=record.html_content)
    assert expected_export_file_count(4, export_markdown=True, export_docx=True, export_pdf=True, export_separate=False, export_merged=True) == 3
    old = path / "old"
    new = path / "new"
    old.mkdir()
    new.mkdir()
    (old / "所选内容.md").write_text("x", encoding="utf-8")
    (old / "所选内容.docx").write_text("x", encoding="utf-8")
    (old / "所选内容.pdf").write_bytes(b"%PDF")
    settings = config.Settings(export_markdown=True, export_docx=True, export_pdf=True, export_separate=False, export_merged=True)
    assert offline_plan_counts(database, [content_id], old, settings) == (3, 3)
    assert offline_plan_counts(database, [content_id], new, settings) == (3, 0)


def test_preview_exact_url_old_directory_and_rejects_escape(env, tmp_path):
    database, path, _session = env
    old = path / "old-output"
    old.mkdir()
    image = old / "image.png"
    image.write_bytes(b"\x89PNG\r\n")
    other = old / "other.png"
    other.write_bytes(b"other")
    record = _record(html='<p>Article body</p><img src="https://example.org/image.png"><img src="https://example.org/missing.png">')
    _action, content_id = database.save_content(record, markdown="body", html=record.html_content)
    database.replace_images(content_id, [
        {"original_url": "https://example.org/image.png", "local_path": str(image), "download_status": "downloaded", "sort_index": 1, "hash": ""},
        {"original_url": "https://example.org/other.png", "local_path": str(other), "download_status": "downloaded", "sort_index": 2, "hash": ""},
    ])
    row = database.get_content(content_id)
    images = [dict(item) for item in database.list_images(content_id)]
    from zhihu_collector.gui.preview_renderer import registered_image_roots
    rendered = render_article_preview(row, images, allowed_roots=[path / "new-output", *registered_image_roots([str(image), str(other)])])
    assert "图片未下载或本地文件缺失" in rendered
    assert image.resolve().as_posix() in rendered
    assert other.resolve().as_posix() not in rendered
    escaped = tmp_path / "secret.png"
    escaped.write_bytes(b"secret")
    malicious = [{"original_url": "https://example.org/image.png", "local_path": str(path / "old-output" / ".." / "secret.png")}]
    blocked = render_article_preview(row, malicious, allowed_roots=registered_image_roots([malicious[0]["local_path"]]))
    assert escaped.resolve().as_posix() not in blocked


def test_service_confirm_then_export_uses_real_manifest(env, monkeypatch):
    database, path, session = env
    answer = _record("42", html="<p>可控正文结尾 MARK42</p>")
    item = _item("12", [answer])
    listener = MagicMock()
    listener.snapshot.return_value = [item]
    listener.consume.side_effect = lambda: [item]
    listener.consume_errors.return_value = []
    listener.last_paging = {"is_end": True, "next": ""}
    page = session.new_page.return_value
    page.content.return_value = "<html><h1>Audit Collection</h1></html>"
    page.url = URL
    monkeypatch.setattr(coll, "NetworkListener", lambda: listener)
    monkeypatch.setattr(coll, "inspect_page", lambda *args, **kwargs: None)
    request = _request(path, collection_scope_confirmation=True)
    service = svc.CollectionService(_fast_settings(), database, TaskController())
    with pytest.raises(CollectionScopeReady) as raised:
        service.run(request)
    assert database.latest_task(URL)["status"] == "awaiting_confirmation"
    assert raised.value.plan.member_ids == ["42"]
    request.confirmed_collection_member_ids = ["42"]
    request.collection_scope_confirmation = True
    listener.consume.side_effect = lambda: [item]
    svc.CollectionService(_fast_settings(), database, TaskController()).run(request)
    stored = database.list_contents()[0]
    assert stored["zhihu_content_id"] == "42"
    assert "MARK42" in stored["html_content"]
    bodies = [file.read_text(encoding="utf-8") for file in (path / "out").rglob("*.md") if file.name != "导出结果.md"]
    assert any("MARK42" in text for text in bodies)
    members = database.get_collection_members("12")
    assert [member["answer_id"] for member in members] == ["42"]


@pytest.fixture
def qtapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_library_preview_reads_old_output_after_settings_change(env, qtapp):
    from PySide6.QtGui import QImage
    from zhihu_collector.gui.library_window import LibraryWindow

    database, path, _session = env
    old = path / "old-output"
    old.mkdir()
    image = old / "image.png"
    qimage = QImage(20, 20, QImage.Format.Format_RGB32)
    qimage.fill(0x0088FF)
    assert qimage.save(str(image))
    record = _record(html='<p>Article body</p><img src="https://example.org/image.png">')
    _action, content_id = database.save_content(record, markdown="body", html=record.html_content)
    database.replace_images(content_id, [{
        "original_url": "https://example.org/image.png",
        "local_path": str(image),
        "download_status": "downloaded",
        "sort_index": 1,
        "hash": "",
    }])
    window = LibraryWindow(database, config.Settings(output_dir=str(path / "new-output")))
    try:
        window.table.selectRow(0)
        qtapp.processEvents()
        assert "图片未下载或本地文件缺失" not in window.preview_browser.toPlainText()
        assert "Article body" in window.preview_browser.toPlainText()
    finally:
        window.close()


def test_invalid_preset_save_shows_warning_and_keeps_memory(env, qtapp, monkeypatch):
    database, path, _session = env
    monkeypatch.setattr(main_gui, "load_settings", lambda: config.Settings(database_path=str(path / "gui.db"), output_dir=str(path / "out")))
    warnings = []
    monkeypatch.setattr(main_gui.QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args))
    monkeypatch.setattr(main_gui.QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_gui.QInputDialog, "getText", lambda *args, **kwargs: ("InvalidPreset", True))
    window = main_gui.MainWindow()
    try:
        original = [preset.to_dict() for preset in window.preset_manager.presets]
        window.markdown_box.setChecked(False)
        window.docx_box.setChecked(False)
        window.pdf_box.setChecked(False)
        window._save_current_preset()
        assert warnings
        assert [preset.to_dict() for preset in window.preset_manager.presets] == original
        assert window.preset_status_label.text() != ""

        warnings.clear()
        window.markdown_box.setChecked(True)
        window.separate_box.setChecked(False)
        window.merged_box.setChecked(False)
        window._save_current_preset()
        assert any("输出方式" in str(item) for item in warnings)

        warnings.clear()
        window.separate_box.setChecked(True)
        monkeypatch.setattr(main_gui.QInputDialog, "getText", lambda *args, **kwargs: ("名" * 51, True))
        window._save_current_preset()
        assert any("50" in str(item) for item in warnings)

        warnings.clear()
        monkeypatch.setattr(main_gui.QInputDialog, "getText", lambda *args, **kwargs: ("可用配置", True))
        monkeypatch.setattr(window.preset_manager, "save", MagicMock(side_effect=OSError("disk full")))
        window._save_current_preset()
        assert any("没有写入成功" in str(item) for item in warnings)
        assert all(preset.name != "可用配置" for preset in window.preset_manager.presets)
    finally:
        window.close()
        database.close()


def test_worker_returns_when_collection_scope_is_ready(qtapp, tmp_path):
    from zhihu_collector.collectors.base import Progress
    from zhihu_collector.gui.worker import CollectWorker

    seen = []

    class FakeService:
        def __init__(self, *args, **kwargs):
            self.progress = Progress()

        def run(self, request):
            raise CollectionScopeReady(ExportPlan(scope_kind="collection_exact", member_ids=["1"], output_root=str(tmp_path)))

    import zhihu_collector.gui.worker as worker_mod
    original = worker_mod.CollectionService
    worker_mod.CollectionService = FakeService
    try:
        worker = CollectWorker(config.Settings(), Database(tmp_path / "worker.db"), _request(tmp_path), TaskController())
        worker.scope_ready.connect(seen.append)
        worker.run()
        assert seen and seen[0].member_ids == ["1"]
    finally:
        worker_mod.CollectionService = original


def test_unknown_items_block_complete_collection_status(env, monkeypatch):
    database, path, _session = env
    manifest = _manifest(monkeypatch, [_record()])
    manifest.unknown_type_count = 1
    manifest.non_answer_count = 2
    service = svc.CollectionService(config.Settings(), database, TaskController())
    service.run(_request(path))
    task = database.latest_task(URL)
    assert task["status"] != "completed" or task["error_message"]
    assert "未识别" in (task["error_message"] or "")
    assert "搜集完成" not in (service.progress.message or "")
    report = next((path / "out").rglob("导出结果.md")).read_text(encoding="utf-8")
    assert "另有1条未识别" in report
    assert "过滤非回答条目**：2" in report
    snapshot = database.get_collection_snapshot(task_id=int(task["id"]))
    assert snapshot["integrity_status"] != "complete"


def test_known_non_answers_still_allow_complete_collection(env, monkeypatch):
    database, path, _session = env
    manifest = _manifest(monkeypatch, [_record()])
    manifest.non_answer_count = 3
    manifest.unknown_type_count = 0
    service = svc.CollectionService(config.Settings(), database, TaskController())
    service.run(_request(path))
    task = database.latest_task(URL)
    assert task["status"] == "completed"
    assert not task["error_message"]
    assert "搜集完成" in (service.progress.message or "")


def test_old_body_retry_keeps_later_merge_until_user_exports_again(env, monkeypatch):
    database, path, _session = env
    first, second, third = _record("1"), _record("2"), _record("3")
    for record in (first, third):
        database.save_content(record, markdown=record.html_content, html=record.html_content)
    old = _request(path, export_separate=False, export_merged=True, confirmed_collection_member_ids=["1", "2"])
    database.record_content_failure(second, None, old.as_options(), "old failed body")
    _manifest(monkeypatch, [first, second, third])
    newer = _request(path, export_separate=False, export_merged=True, confirmed_collection_member_ids=["3"])
    svc.CollectionService(config.Settings(), database, TaskController()).run(newer)
    target = next(item for item in (path / "out").rglob("*.md") if item.name != "导出结果.md")
    before = target.read_bytes()
    assert b"FULL BODY 3" in before
    retry_body_failures(database, config.Settings())
    assert target.read_bytes() == before
    assert any(row["zhihu_content_id"] == "2" for row in database.list_contents())
    assert not [item for item in database.list_failures() if item["kind"] == "body"]
    fresh = _request(path, export_separate=False, export_merged=True, confirmed_collection_member_ids=["1", "2"])
    svc.CollectionService(config.Settings(), database, TaskController()).run(fresh)
    replaced = target.read_bytes()
    assert replaced != before
    assert b"FULL BODY 1" in replaced and b"FULL BODY 2" in replaced


def test_update_only_is_part_of_saved_preset():
    preset = Preset(id="custom", name="增量", update_only=True, export_markdown=True, export_separate=True)
    preset.validate()
    assert preset.to_dict()["update_only"] is True
