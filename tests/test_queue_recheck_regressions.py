"""0.1.0 独立复验发现的队列问题回归（2026-10-13）。"""

import json
import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

import zhihu_collector.config as config
from zhihu_collector.collectors.service import CollectRequest
from zhihu_collector.config import Settings
from zhihu_collector.database import catalog
from zhihu_collector.database.database import Database

ITEM = {"request_json": "{}", "dedupe_key": "user:audit|answers"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "user_data_root", lambda: tmp_path)
    settings = Settings(database_path=str(tmp_path / "db.sqlite"), output_dir=str(tmp_path / "out"))
    db = Database(tmp_path / "db.sqlite")
    yield settings, db
    db.close()


def _waiting(db):
    return [r for r in catalog.list_queue_items(db) if r["status"] == "waiting"]


def test_resume_after_restart_does_not_duplicate_reenqueued_target(setup):
    _, db = setup
    first = catalog.enqueue_requests(db, [ITEM], batch_id="a")
    catalog.update_queue_item(db, first[0], status="running")
    db.recover_interrupted_tasks()
    catalog.enqueue_requests(db, [ITEM], batch_id="b")
    catalog.reopen_queue_statuses(db, ("interrupted", "needs_login", "needs_verify"))
    assert len(_waiting(db)) == 1
    old = next(r for r in catalog.list_queue_items(db) if int(r["id"]) == first[0])
    assert old["status"] == "cancelled"
    assert old["error_message"] == catalog.SUPERSEDED_MESSAGE


def test_resume_two_interrupted_same_target_keeps_one(setup):
    _, db = setup
    a = catalog.enqueue_requests(db, [ITEM], batch_id="a")[0]
    catalog.update_queue_item(db, a, status="interrupted")
    b = catalog.enqueue_requests(db, [ITEM], batch_id="b")[0]
    catalog.update_queue_item(db, b, status="interrupted")
    assert catalog.reopen_queue_statuses(db, ("interrupted",)) == 1
    assert [int(r["id"]) for r in _waiting(db)] == [a]


def test_resume_needs_login_item_still_reopens(setup):
    _, db = setup
    a = catalog.enqueue_requests(db, [ITEM], batch_id="a")[0]
    catalog.update_queue_item(db, a, status="needs_login")
    assert catalog.reopen_queue_statuses(db, ("interrupted", "needs_login", "needs_verify")) == 1
    assert [int(r["id"]) for r in _waiting(db)] == [a]


def test_retry_failed_refused_while_same_target_waiting(setup):
    _, db = setup
    first = catalog.enqueue_requests(db, [ITEM], batch_id="a")
    catalog.update_queue_item(db, first[0], status="failed")
    catalog.enqueue_requests(db, [ITEM], batch_id="b")
    assert catalog.retry_failed_item(db, first[0]) is False
    assert len(_waiting(db)) == 1


def test_retry_failed_allowed_when_target_not_queued(setup):
    _, db = setup
    first = catalog.enqueue_requests(db, [ITEM], batch_id="a")
    catalog.update_queue_item(db, first[0], status="failed")
    assert catalog.retry_failed_item(db, first[0]) is True
    assert len(_waiting(db)) == 1


def _window(setup, tmp_path, monkeypatch):
    settings, _ = setup
    import zhihu_collector.gui.main_window as mw
    from zhihu_collector.presets import PresetManager

    monkeypatch.setattr(mw, "load_settings", lambda: settings)
    monkeypatch.setattr(mw, "PresetManager", lambda: PresetManager(tmp_path / "presets.json"))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)
    app = QApplication.instance() or QApplication([])
    return app, mw.MainWindow()


def _finish(w, *, noted=None, found=10, completed=7):
    ids = w._enqueue_requests([CollectRequest(url="https://www.zhihu.com/people/audit", mode="answers", execution_intent="library")])
    item = w.queue.try_begin()
    tid = w.database.create_task(task_type="user_answers", target_url="u", target_id="audit", output_dir="", options={})
    w.database.update_task(tid, status="unconfirmed", total_found=found, total_completed=completed)
    fields = {"task_id": tid}
    if noted is not None:
        fields["result_json"] = json.dumps(noted)
    catalog.update_queue_item(w.database, ids[0], **fields)
    w._current_queue_id = int(item["id"])
    w._finish_current_from_task()
    return json.loads(catalog.list_queue_items(w.database)[0]["result_json"])


@pytest.mark.parametrize(
    "noted, expected",
    [(None, 0), ({"failed": 0}, 0), ({"failed": 2, "images_failed": 1, "exports_failed": 0}, 2)],
)
def test_queue_failed_count_uses_recorded_failures(setup, tmp_path, monkeypatch, noted, expected):
    app, w = _window(setup, tmp_path, monkeypatch)
    try:
        result = _finish(w, noted=noted)
        assert result["failed"] == expected
        if noted and "images_failed" in noted:
            assert result["images_failed"] == 1
    finally:
        w.close()
        app.processEvents()
        w.database.close()
