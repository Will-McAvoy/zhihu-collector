"""第8轮：批量队列、常用来源、内容库更新和本地整理。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.listing import select_records_to_emit
from zhihu_collector.collectors.paging import ScrollState
from zhihu_collector.collectors.queue_session import QueueSession, drive_queue
from zhihu_collector.collectors.service import (
    CollectRequest,
    collect_request_from_snapshot,
    exports_user_files,
    listing_uses_incremental_stop,
    write_separate_exports,
)
from zhihu_collector.config import Settings
from zhihu_collector.database.catalog import (
    add_tags,
    content_tags,
    count_source_contents,
    delete_source,
    enqueue_requests,
    fetch_source,
    list_queue_items,
    move_source_order,
    note_source_result,
    record_task_content,
    remove_tags,
    save_source,
    set_read_state,
    set_starred,
    task_content_ids,
    update_source,
    user_state,
)
from zhihu_collector.database.database import Database
from zhihu_collector.database.migrations import MIGRATIONS
from zhihu_collector.errors import CollectionScopeReady, LoginExpiredError
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.presets import Preset
from zhihu_collector.utils.batch_targets import history_source_candidates, parse_batch
from zhihu_collector.utils.time_utils import now_iso


def _record(content_id, html, *, author="author-a", name="作者A", updated=1700001000, votes=1, question="9"):
    return ContentRecord(
        zhihu_content_id=str(content_id),
        content_type="answer",
        url=f"https://www.zhihu.com/question/{question}/answer/{content_id}",
        title=f"标题{content_id}",
        author=AuthorInfo(name=name, url_token=author, url=f"https://www.zhihu.com/people/{author}", zhihu_user_id=author),
        question_id=question,
        question_title=f"问题{question}",
        question_url=f"https://www.zhihu.com/question/{question}",
        html_content=html,
        updated_time=updated,
        vote_count=votes,
    )


def _save(database, record):
    return database.save_content(record, markdown=record.html_content, html=record.html_content)


def test_batch_parser_splits_noise_and_keeps_answer_identity():
    text = "\n".join([
        "",
        "推荐阅读 https://www.zhihu.com/question/12/answer/99 ，很好",
        "https://www.zhihu.com/answer/99?utm=1#top",
        "https://www.zhihu.com/people/author-a/answers",
        "https://www.zhihu.com/people/author-a/pins",
        "https://evil.example/people/author-a",
        "https://zhuanlan.zhihu.com/p/1",
        "https://www.zhihu.com/question/12",
        "   ",
    ])
    items = parse_batch(text, default_mode="auto")
    reasons = [item.reason for item in items]
    assert any(item.reason == "空行" for item in items)
    answer_items = [item for item in items if item.identity_key == "answer:99"]
    assert len(answer_items) == 2
    assert answer_items[0].valid is True
    assert answer_items[0].mode == "single_answer"
    assert answer_items[1].valid is False
    assert "同一个目标" in answer_items[1].reason
    assert any(item.kind == "unknown" and "不会被当作" in item.reason for item in items)
    user_items = [item for item in items if item.identity_key.startswith("user:")]
    assert {item.mode for item in user_items} == {"all"}
    # 显式不同范围不合并
    scoped = parse_batch(
        "https://www.zhihu.com/people/author-a\nhttps://www.zhihu.com/people/author-a",
        default_mode="answers",
    )
    pins = parse_batch("https://www.zhihu.com/people/author-a", default_mode="pins")
    assert scoped[0].dedupe_key != pins[0].dedupe_key
    assert all(item.mode != "question" for item in answer_items)


def test_batch_reports_queue_conflict_without_dropping_valid_rows(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    request = CollectRequest(
        url="https://www.zhihu.com/question/1/answer/5",
        mode="single_answer",
        execution_intent="library",
        max_items=5,
    )
    enqueue_requests(database, [{
        "request_json": json.dumps(request.snapshot(), ensure_ascii=False),
        "dedupe_key": "answer:5|single_answer",
        "source_id": None,
    }], batch_id="b1")
    active = {row["dedupe_key"]: row for row in list_queue_items(database)}
    items = parse_batch(
        "https://www.zhihu.com/answer/5\nhttps://www.zhihu.com/question/8",
        default_mode="auto",
        active_keys=active,
        option_signature={"max_items": 9, "execution_intent": "export"},
    )
    assert items[0].valid is False
    assert "已在队列" in items[0].reason
    assert "篇数上限" in items[0].reason
    assert items[1].valid is True
    database.close()


def test_queue_snapshot_ignores_later_preset_edits(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    request = CollectRequest(
        url="https://www.zhihu.com/people/author-a",
        mode="answers",
        execution_intent="library",
        max_items=5,
        export_markdown=True,
    )
    enqueue_requests(database, [{
        "request_json": json.dumps(request.snapshot(), ensure_ascii=False),
        "dedupe_key": "user:people:author-a|answers",
        "source_id": None,
    }], batch_id="batch")
    request.max_items = 100
    request.execution_intent = "export"
    stored = json.loads(list_queue_items(database)[0]["request_json"])
    assert stored["max_items"] == 5
    assert stored["execution_intent"] == "library"
    restored = collect_request_from_snapshot(stored)
    assert restored.max_items == 5
    database.close()


def test_queue_is_serial_and_login_stops_the_rest(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    items = []
    for index, name in enumerate("abc"):
        items.append({
            "request_json": json.dumps({"url": name, "mode": "answers"}),
            "dedupe_key": f"user:people:{name}|answers",
            "source_id": None,
        })
    enqueue_requests(database, items, batch_id="run")
    seen = []
    current = {"value": 0}
    maximum = {"value": 0}

    def runner(item):
        current["value"] += 1
        maximum["value"] = max(maximum["value"], current["value"])
        time.sleep(0.02)
        current["value"] -= 1
        seen.append(json.loads(item["request_json"])["url"])
        if json.loads(item["request_json"])["url"] == "a":
            raise LoginExpiredError("需要登录")
        return {"status": "success"}

    session = QueueSession(database)
    drive_queue(session, runner)
    assert seen == ["a"]
    assert maximum["value"] == 1
    statuses = [row["status"] for row in list_queue_items(database)]
    assert statuses[0] == "needs_login"
    assert statuses[1:] == ["waiting", "waiting"]
    database.close()


def test_queue_failure_continues_pause_stop_and_cancel(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    enqueue_requests(database, [
        {"request_json": json.dumps({"url": str(i)}), "dedupe_key": f"answer:{i}|single_answer", "source_id": None}
        for i in range(3)
    ], batch_id="life")
    session = QueueSession(database)

    def fail_first(item):
        if json.loads(item["request_json"])["url"] == "0":
            raise RuntimeError("普通失败")
        return {"status": "success"}

    drive_queue(session, fail_first)
    statuses = [row["status"] for row in list_queue_items(database)]
    assert statuses == ["failed", "success", "success"]

    database2 = Database(tmp_path / "pause.db")
    enqueue_requests(database2, [
        {"request_json": json.dumps({"url": name}), "dedupe_key": f"answer:{name}|single_answer", "source_id": None}
        for name in ("a", "b", "c")
    ], batch_id="pause")
    paused = QueueSession(database2)
    order = []

    def runner(item):
        order.append(json.loads(item["request_json"])["url"])
        return {"status": "success"}

    paused.hold = True
    assert drive_queue(paused, runner) == []
    paused.hold = False
    drive_queue(paused, runner)
    assert order == ["a", "b", "c"]

    database3 = Database(tmp_path / "stop.db")
    enqueue_requests(database3, [
        {"request_json": json.dumps({"url": name}), "dedupe_key": f"answer:{name}|single_answer", "source_id": None}
        for name in ("a", "b")
    ], batch_id="stop")
    stopped = QueueSession(database3)
    first = stopped.try_begin()
    assert stopped.try_begin() is None
    stopped.hold = True
    stopped.finish(int(first["id"]), "interrupted", "停止队列")
    assert stopped.try_begin() is None
    waiting = [row["status"] for row in list_queue_items(database3)]
    assert waiting == ["interrupted", "waiting"]
    database.close()
    database2.close()
    database3.close()


def test_queue_collection_confirmation_branches(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    enqueue_requests(database, [
        {"request_json": "{}", "dedupe_key": "collection:1|collection", "source_id": None},
        {"request_json": "{}", "dedupe_key": "answer:2|single_answer", "source_id": None},
    ], batch_id="col")
    session = QueueSession(database)

    def confirm(item):
        raise CollectionScopeReady(object())

    done = drive_queue(session, confirm)
    assert len(done) == 1
    assert list_queue_items(database)[0]["status"] == "awaiting_confirm"
    assert list_queue_items(database)[1]["status"] == "waiting"
    database.close()


def test_parallel_drivers_never_overlap(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    enqueue_requests(database, [
        {"request_json": json.dumps({"url": str(i)}), "dedupe_key": f"answer:{i}|single_answer", "source_id": None}
        for i in range(12)
    ], batch_id="parallel")
    session = QueueSession(database)
    current = {"value": 0}
    maximum = {"value": 0}
    lock = threading.Lock()

    def runner(item):
        with lock:
            current["value"] += 1
            maximum["value"] = max(maximum["value"], current["value"])
        time.sleep(0.01)
        with lock:
            current["value"] -= 1
        return {"status": "success"}

    threads = [threading.Thread(target=drive_queue, args=(session, runner)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum["value"] == 1
    assert all(row["status"] == "success" for row in list_queue_items(database))
    database.close()


def test_restart_marks_running_queue_interrupted_and_does_not_autostart(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    enqueue_requests(database, [{
        "request_json": json.dumps({"url": "https://www.zhihu.com/people/author-a", "mode": "answers"}),
        "dedupe_key": "user:people:author-a|answers",
        "source_id": None,
    }], batch_id="crash")
    item = database.claim_next_queue_item() if hasattr(database, "claim_next_queue_item") else None
    from zhihu_collector.database.catalog import claim_next_queue_item
    item = claim_next_queue_item(database)
    assert item["status"] == "running"
    database.recover_interrupted_tasks()
    restored = list_queue_items(database)[0]
    assert restored["status"] == "interrupted"
    assert json.loads(restored["request_json"])["url"].endswith("author-a")
    session = QueueSession(database)
    assert session.try_begin() is None
    database.close()


def test_duplicate_finish_does_not_release_two_items(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    enqueue_requests(database, [
        {"request_json": "{}", "dedupe_key": f"answer:{i}|single_answer", "source_id": None}
        for i in (1, 2)
    ], batch_id="once")
    session = QueueSession(database)
    first = session.try_begin()
    assert session.finish(int(first["id"]), "success") is True
    assert session.finish(int(first["id"]), "success") is False
    second = session.try_begin()
    assert int(second["id"]) != int(first["id"])
    database.close()


def test_history_import_links_existing_author_without_nickname_mixup(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    for index in range(49):
        _save(database, _record(index + 1, f"<p>旧正文{index}</p>", author="author-a", name="重名"))
    _save(database, _record(900, "<p>另一个</p>", author="author-b", name="重名"))
    database.create_task(
        task_type="user_answers",
        target_url="https://www.zhihu.com/people/author-a",
        target_id="author-a",
        output_dir=str(tmp_path),
        options={"url": "https://www.zhihu.com/people/author-a", "mode": "answers"},
    )
    database.create_task(
        task_type="user_answers",
        target_url="https://www.zhihu.com/people/author-b",
        target_id="author-b",
        output_dir=str(tmp_path),
        options={"url": "https://www.zhihu.com/people/author-b", "mode": "answers"},
    )
    candidates = history_source_candidates(database)
    assert len(candidates) == 2
    for candidate in candidates:
        source_id, created = save_source(
            database,
            display_name=candidate["canonical_url"],
            canonical_url=candidate["canonical_url"],
            target_kind=candidate["target_kind"],
            identity_key=candidate["identity_key"],
            collect_mode=candidate["collect_mode"],
            preset_snapshot={},
        )
        assert created is True
        source = next(row for row in database._conn.execute("SELECT * FROM sources"))
    rows = list(database._conn.execute("SELECT * FROM sources ORDER BY id"))
    counts = [count_source_contents(database, row) for row in rows]
    assert sorted(counts) == [1, 49]
    database.close()


def test_removing_source_or_preset_keeps_content_and_queue_snapshot(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    _save(database, _record(1, "<p>保留</p>"))
    source_id, _created = save_source(
        database,
        display_name="https://www.zhihu.com/people/author-a",
        canonical_url="https://www.zhihu.com/people/author-a",
        target_kind="user",
        identity_key="user:people:author-a",
        collect_mode="answers",
        preset_id="missing",
        preset_snapshot={"execution_intent": "library", "max_items": 3},
    )
    request = CollectRequest(
        url="https://www.zhihu.com/people/author-a",
        mode="answers",
        execution_intent="library",
        max_items=3,
        source_id=source_id,
    )
    enqueue_requests(database, [{
        "request_json": json.dumps(request.snapshot(), ensure_ascii=False),
        "dedupe_key": "user:people:author-a|answers",
        "source_id": source_id,
    }], batch_id="keep")
    delete_source(database, source_id)
    assert database.count_contents() == 1
    stored = json.loads(list_queue_items(database)[0]["request_json"])
    assert stored["max_items"] == 3
    assert stored["execution_intent"] == "library"
    assert list_queue_items(database)[0]["source_id"] is None
    database.close()


def test_library_update_counts_new_changed_and_reused_without_export(tmp_path, monkeypatch):
    database = Database(tmp_path / "zhihu.db")
    for index in range(49):
        html = "<p>旧正文</p>" if index < 47 else "<p>将被修改</p>"
        _save(database, _record(index + 1, html, updated=1700001000, votes=4))
    changed = []
    for index in (48, 49):
        record = _record(index, "<p>新正文</p>", updated=1700002000, votes=4)
        changed.append(record)
    fresh = [_record(50 + offset, f"<p>新增{offset}</p>", updated=1700003000) for offset in range(3)]
    actions = []
    for record in [*(_record(i, "<p>旧正文</p>", updated=1700001000, votes=9) for i in range(1, 48)), *changed, *fresh]:
        actions.append(_save(database, record)[0])
    assert database.count_contents() == 52
    assert actions.count("inserted") == 3
    assert actions.count("updated") == 2
    assert actions.count("metadata") == 47
    untouched = _record(1, "<p>旧正文</p>", updated=1700001000, votes=9)
    assert _save(database, untouched)[0] == "skipped"
    incomplete = _record(1, "", updated=1700004000)
    incomplete.content_incomplete = True
    assert _save(database, incomplete)[0] == "skipped"
    assert "旧正文" in database.list_contents(zhihu_content_id="1")[0]["html_content"]
    assert exports_user_files(CollectRequest(url="https://www.zhihu.com/people/author-a")) is True
    assert exports_user_files(CollectRequest(
        url="https://www.zhihu.com/people/author-a",
        execution_intent="library",
        export_markdown=True,
        export_docx=True,
        export_pdf=True,
    )) is False
    assert listing_uses_incremental_stop(CollectRequest(url="x", update_only=True)) is True
    assert listing_uses_incremental_stop(CollectRequest(url="x", execution_intent="library", update_only=True)) is False
    database.close()


def test_incremental_stop_would_miss_a_later_edit_but_full_scan_does_not():
    old = _record("1", "<p>旧</p>")
    changed = _record("9", "<p>后面改了</p>")
    known = {"1": (old.updated_time and __import__("zhihu_collector.utils.time_utils", fromlist=["unix_to_iso"]).unix_to_iso(old.updated_time), old.content_hash)}
    from zhihu_collector.collectors.listing import _version
    known = {"1": _version(old)}
    early = ScrollState()
    select_records_to_emit([old], state=early, known_versions=known, update_only=True, sorted_by_time=True, has_next=True)
    assert early.reached_end is True
    scanning = ScrollState()
    select_records_to_emit([old], state=scanning, known_versions=known, update_only=False, sorted_by_time=True, has_next=True)
    emitted = select_records_to_emit([changed], state=scanning, known_versions=known, update_only=False, sorted_by_time=True, has_next=True)
    assert scanning.reached_end is False
    assert [item.zhihu_content_id for item in emitted] == ["9"]


def test_absence_does_not_delete_local_rows_and_gap_is_not_success(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    for index in range(49):
        _save(database, _record(index + 1, "<p>仍在</p>"))
    for index in range(44):
        _save(database, _record(index + 1, "<p>仍在</p>"))
    assert database.count_contents() == 49
    source_id, _created = save_source(
        database,
        display_name="https://www.zhihu.com/people/author-a",
        canonical_url="https://www.zhihu.com/people/author-a",
        target_kind="user",
        identity_key="user:people:author-a",
        collect_mode="answers",
    )
    note_source_result(
        database,
        source_id,
        status="unconfirmed",
        task_id=None,
        succeeded=False,
        result={"scanned": 49, "declared_total": 54},
    )
    source = database._conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert source["last_status"] == "unconfirmed"
    assert source["last_success_at"] in (None, "")
    assert source["last_attempt_at"]
    database.close()


def test_tags_stars_and_read_state_survive_body_update(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    _local, first = _save(database, _record(1, "<p>原文</p>"))
    _local, second = _save(database, _record(2, "<p>另一篇</p>", author="author-b", name="作者B"))
    saved = add_tags(database, [first, second], [" 投资 ", "投资", "", "  "])
    assert saved == ["投资"]
    set_starred(database, [first], starred=True)
    set_read_state(database, [first], is_read=True)
    updated = _record(1, "<p>改后的正文</p>", updated=1700002222)
    assert _save(database, updated)[0] == "updated"
    read, starred, tags = user_state(database, first)
    assert read is True and starred is True and tags == ["投资"]
    remove_tags(database, [first], ["投资"])
    assert content_tags(database, first) == []
    rows, total = database.search_contents(tag="投资", read_state="unread")
    assert total == 1
    assert rows[0]["id"] == second
    rows, total = database.search_contents(keyword="改后的正文", sort="published_desc")
    assert total == 1
    found, total = database.search_contents(keyword="改后的正文")
    assert total == 1
    database.close()


def test_filters_cover_the_whole_library_and_keep_page_stable(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    for index in range(25):
        record = _record(index + 1, f"<p>关键词甲{index}</p>" if index % 2 == 0 else "<p>其他</p>", votes=3)
        _save(database, record)
    rows, total = database.search_contents(keyword="关键词甲", limit=20, offset=0, sort="votes_desc")
    assert total == 13
    assert len(rows) == 13 or len(rows) == 20
    assert len(rows) == 13
    page, _total = database.search_contents(sort="votes_desc", limit=2, offset=0)
    page2, _total = database.search_contents(sort="votes_desc", limit=2, offset=2)
    ids = [row["id"] for row in page] + [row["id"] for row in page2]
    assert len(ids) == len(set(ids)) == 4
    database.close()


def test_library_only_service_does_not_touch_export_files(tmp_path, monkeypatch):
    from zhihu_collector.collectors import service as service_module

    database = Database(tmp_path / "zhihu.db")
    output = tmp_path / "output"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    record = _record(7, "<p>仅入库</p><img src='https://pic.zhimg.com/a.jpg'/>")
    calls = {"export": 0, "pdf": 0}

    def fail_export(*_args, **_kwargs):
        calls["export"] += 1
        raise AssertionError("仅更新内容库不应写导出文件")

    class BoomPdf:
        def __init__(self):
            calls["pdf"] += 1

        def close(self):
            return None

    monkeypatch.setattr(service_module, "write_separate_exports", fail_export)
    monkeypatch.setattr(service_module, "PdfExporter", BoomPdf)
    monkeypatch.setattr(service_module, "BrowserSession", _FakeSession)
    monkeypatch.setattr(service_module, "export_saved_rows", fail_export)
    service = service_module.CollectionService(Settings(database_path=str(database.path)), database, TaskController())

    def fake_collect(session, parsed, request, output_root, downloader, pdf):
        service._persist_record(record, output_root, request, None, pdf, index_scope="single")

    monkeypatch.setattr(service, "_collect_single_answer", fake_collect)
    request = CollectRequest(
        url="https://www.zhihu.com/question/9/answer/7",
        mode="single_answer",
        output_dir=str(output),
        execution_intent="library",
        export_markdown=True,
        export_docx=True,
        export_pdf=True,
        export_separate=True,
        download_images=False,
    )
    service.run(request)
    assert calls["export"] == 0
    assert calls["pdf"] == 0
    assert marker.read_text(encoding="utf-8") == "keep"
    assert list(output.iterdir()) == [marker]
    saved = database.list_contents(zhihu_content_id="7")[0]
    assert "仅入库" in saved["html_content"]
    assert task_content_ids(database, service.task_id, "inserted") == [saved["id"]]
    database.close()


class _FakeSession:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_skip_existing_file_is_not_counted_as_a_new_write(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    _action, local_id = _save(database, _record(3, "<p>正文</p>"))
    row = database.get_content(local_id)
    settings = Settings()
    first = CollectRequest(url="https://www.zhihu.com/people/author-a", export_markdown=True, export_docx=False, export_pdf=False)
    directory = tmp_path / "回答"
    result = write_separate_exports(row, [], directory, first, settings, None, database)
    assert result["markdown"] == "success"
    target = next(directory.glob("*.md"))
    before = target.read_text(encoding="utf-8")
    skipped = CollectRequest(
        url="https://www.zhihu.com/people/author-a",
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
        conflict_policy="skip",
    )
    again = write_separate_exports(row, [], directory, skipped, settings, None, database)
    assert again["markdown"] == "skipped"
    assert target.read_text(encoding="utf-8") == before
    database.close()


def test_stale_export_is_visible_after_body_update(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    _action, local_id = _save(database, _record(4, "<p>第一版</p>"))
    row = database.get_content(local_id)
    settings = Settings()
    request = CollectRequest(url="x", export_markdown=True, export_docx=False, export_pdf=False)
    write_separate_exports(row, [], tmp_path / "回答", request, settings, None, database)
    changed = _record(4, "<p>第二版</p>", updated=1800000000)
    assert _save(database, changed)[0] == "updated"
    rows, total = database.search_contents()
    assert total == 1
    assert rows[0]["export_stale"] == 1
    database.close()


def test_migration_keeps_old_rows_and_defaults_user_state(tmp_path):
    path = tmp_path / "old.db"
    import sqlite3
    conn = sqlite3.connect(path)
    for version, script in MIGRATIONS:
        if version >= 7:
            break
        conn.executescript(script)
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, now_iso()),
        )
    conn.execute(
        """
        INSERT INTO contents(
            zhihu_content_id, content_type, title, url, html_content, markdown_content, content_hash, author_name
        ) VALUES ('42', 'answer', '旧标题', 'https://www.zhihu.com/question/1/answer/42', '<p>旧</p>', '旧', 'hash', '旧作者')
        """
    )
    conn.commit()
    conn.close()
    database = Database(path)
    assert database.count_contents() == 1
    rows, total = database.search_contents()
    assert total == 1
    assert rows[0]["is_read"] == 0
    assert rows[0]["starred"] == 0
    assert database._conn.execute("SELECT name FROM sqlite_master WHERE name='sources'").fetchone()
    database.close()


def test_many_queue_items_and_library_rows(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    with database._lock:
        database._conn.execute("BEGIN")
        for index in range(5000):
            database._conn.execute(
                """
                INSERT INTO contents(
                    zhihu_content_id, content_type, title, url, html_content, markdown_content,
                    content_hash, author_name, vote_count, created_time
                ) VALUES (?, 'answer', ?, ?, ?, ?, ?, '批量作者', ?, ?)
                """,
                (
                    str(index + 1),
                    "投资笔记" if index == 0 else f"标题{index}",
                    f"https://www.zhihu.com/question/1/answer/{index + 1}",
                    "<p>投资正文</p>" if index == 0 else "<p>普通</p>",
                    "投资正文" if index == 0 else "普通",
                    f"hash-{index}",
                    index % 7,
                    f"2024-01-{(index % 27) + 1:02d}",
                ),
            )
        database._conn.commit()
    rows, total = database.search_contents(keyword="投资", limit=20)
    assert total == 1
    assert rows[0]["title"] == "投资笔记"
    enqueue_requests(database, [
        {"request_json": json.dumps({"url": str(index)}), "dedupe_key": f"answer:{index}|single_answer", "source_id": None}
        for index in range(100)
    ], batch_id="bulk")
    session = QueueSession(database)
    drive_queue(session, lambda _item: {"status": "success"})
    assert len(list_queue_items(database)) == 100
    assert all(row["status"] == "success" for row in list_queue_items(database))
    database.close()


def test_old_preset_still_requires_an_export_format():
    with pytest.raises(ValueError):
        Preset(id="old", name="旧配置", export_markdown=False, export_docx=False, export_pdf=False).validate()
    Preset(
        id="lib",
        name="只入库",
        execution_intent="library",
        export_markdown=False,
        export_docx=False,
        export_pdf=False,
        export_separate=False,
        export_merged=False,
    ).validate()


def test_shared_answer_state_is_one_row_for_two_sources(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    _action, local_id = _save(database, _record(8, "<p>共享</p>"))
    author_id, _created = save_source(
        database,
        display_name="作者",
        canonical_url="https://www.zhihu.com/people/author-a",
        target_kind="user",
        identity_key="user:people:author-a",
        collect_mode="answers",
    )
    database.save_collection("77", "收藏", "https://www.zhihu.com/collection/77")
    database.save_collection_members("77", [{"answer_id": "8", "content_id": local_id, "sort_order": 1}])
    collection_id, _created = save_source(
        database,
        display_name="收藏",
        canonical_url="https://www.zhihu.com/collection/77",
        target_kind="collection",
        identity_key="collection:77",
        collect_mode="collection",
    )
    add_tags(database, [local_id], ["投资"])
    author = database._conn.execute("SELECT * FROM sources WHERE id = ?", (author_id,)).fetchone()
    collection = database._conn.execute("SELECT * FROM sources WHERE id = ?", (collection_id,)).fetchone()
    assert count_source_contents(database, author) == 1
    assert count_source_contents(database, collection) == 1
    assert database.count_contents() == 1
    rows, total = database.search_contents(source_id=collection_id, tag="投资")
    assert total == 1
    assert rows[0]["id"] == local_id
    database.close()


def test_move_source_order_and_update_source(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    s1, _ = save_source(database, display_name="来源1", canonical_url="https://www.zhihu.com/people/user1", target_kind="user", identity_key="user:user1", collect_mode="answers")
    s2, _ = save_source(database, display_name="来源2", canonical_url="https://www.zhihu.com/people/user2", target_kind="user", identity_key="user:user2", collect_mode="answers")
    s3, _ = save_source(database, display_name="来源3", canonical_url="https://www.zhihu.com/people/user3", target_kind="user", identity_key="user:user3", collect_mode="answers")

    # 初始排序检查
    rows = database._conn.execute("SELECT id, sort_order FROM sources ORDER BY sort_order").fetchall()
    assert [int(r["id"]) for r in rows] == [s1, s2, s3]

    # 上移 s2 -> 此时顺序应为 s2, s1, s3
    assert move_source_order(database, s2, -1) is True
    rows = database._conn.execute("SELECT id, sort_order FROM sources ORDER BY sort_order").fetchall()
    assert [int(r["id"]) for r in rows] == [s2, s1, s3]
    assert [int(r["sort_order"]) for r in rows] == [1, 2, 3]

    # s2 已经是第一位，再上移返回 False
    assert move_source_order(database, s2, -1) is False

    # s3 已经是最后一位，下移返回 False
    assert move_source_order(database, s3, 1) is False

    # s3 上移一位 -> 此时顺序为 s2, s3, s1
    assert move_source_order(database, s3, -1) is True
    rows = database._conn.execute("SELECT id, sort_order FROM sources ORDER BY sort_order").fetchall()
    assert [int(r["id"]) for r in rows] == [s2, s3, s1]

    # 测试 update_source
    update_source(
        database,
        s1,
        display_name="新来源1名称",
        collect_mode="pins",
        preset_id="custom_preset",
        preset_snapshot={"execution_intent": "library", "max_count": 10},
    )
    updated_s1 = fetch_source(database, s1)
    assert updated_s1 is not None
    assert updated_s1["display_name"] == "新来源1名称"
    assert updated_s1["collect_mode"] == "pins"
    assert updated_s1["preset_id"] == "custom_preset"
    snap = json.loads(updated_s1["preset_snapshot_json"])
    assert snap["execution_intent"] == "library"
    assert snap["max_count"] == 10
    database.close()


def test_search_contents_task_id_and_task_outcome_filters(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    _, id1 = _save(database, _record(101, "<p>内容1</p>"))
    _, id2 = _save(database, _record(102, "<p>内容2</p>"))
    _, id3 = _save(database, _record(103, "<p>内容3</p>"))

    task_a = 901
    task_b = 902
    record_task_content(database, task_a, id1, "inserted")
    record_task_content(database, task_a, id2, "updated")
    record_task_content(database, task_a, id3, "reused")
    record_task_content(database, task_b, id1, "updated")

    # task_a 全部 3 个
    rows, total = database.search_contents(task_id=task_a)
    assert total == 3
    assert {r["id"] for r in rows} == {id1, id2, id3}

    # task_a 仅本次新增 (inserted)
    rows, total = database.search_contents(task_id=task_a, task_outcome="inserted")
    assert total == 1
    assert rows[0]["id"] == id1

    # task_a 仅本次更新 (updated)
    rows, total = database.search_contents(task_id=task_a, task_outcome="updated")
    assert total == 1
    assert rows[0]["id"] == id2

    # task_a 复用 (reused)
    rows, total = database.search_contents(task_id=task_a, task_outcome="reused")
    assert total == 1
    assert rows[0]["id"] == id3

    # task_b 仅本次更新 (updated)
    rows, total = database.search_contents(task_id=task_b, task_outcome="updated")
    assert total == 1
    assert rows[0]["id"] == id1

    # 不存在的 outcome
    rows, total = database.search_contents(task_id=task_a, task_outcome="nonexistent")
    assert total == 0
    database.close()
