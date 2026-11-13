"""第8轮验收探针自动化测试（正式集成至测试套件）"""

import json
import os
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

import zhihu_collector.config as config
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.service import CollectionService, CollectRequest, write_separate_exports
from zhihu_collector.config import Settings
from zhihu_collector.database import catalog
from zhihu_collector.database.database import Database
from zhihu_collector.models import AuthorInfo, ContentRecord


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "user_data_root", lambda: tmp_path)
    settings = Settings(database_path=str(tmp_path / "db.sqlite"), output_dir=str(tmp_path / "out"))
    db = Database(tmp_path / "db.sqlite")
    yield settings, db
    db.close()


def record(body):
    return ContentRecord(
        zhihu_content_id="101",
        content_type="answer",
        url="https://www.zhihu.com/question/9/answer/101",
        title="audit",
        html_content=f"<p>{body}</p>",
        author=AuthorInfo(name="audit", url="https://www.zhihu.com/people/audit", zhihu_user_id="audit"),
    )


def service_save(settings, db, tmp_path, body, intent="library", policy="new_copy"):
    req = CollectRequest(
        url="https://www.zhihu.com/people/audit",
        mode="answers",
        execution_intent=intent,
        download_images=False,
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
        conflict_policy=policy,
    )
    svc = CollectionService(settings, db, TaskController())
    svc.task_id = db.create_task(
        task_type="user_answers",
        target_url=req.url,
        target_id="audit",
        output_dir=str(tmp_path / "out"),
        options=req.as_options(),
    )
    result = svc._persist_record(record(body), tmp_path / "out", req, None, None, index_scope="audit")
    return svc.task_id, result


def test_actual_body_update_is_visible_in_update_filter(setup, tmp_path):
    settings, db = setup
    service_save(settings, db, tmp_path, "first body")
    tid, action = service_save(settings, db, tmp_path, "changed body")
    assert action == "updated"
    rows, total = db.search_contents(task_id=tid, task_outcome="updated")
    actual = [dict(r) for r in db._conn.execute("select * from task_content_results where task_id=?", (tid,))]
    assert total == 1, actual


def test_new_copy_does_not_overwrite_prior_export(setup, tmp_path):
    settings, db = setup
    service_save(settings, db, tmp_path, "original text", intent="export")
    files = list((tmp_path / "out").rglob("*.md"))
    assert len(files) == 1
    old = files[0]
    before = old.read_bytes()
    service_save(settings, db, tmp_path, "changed text", intent="export")
    assert old.read_bytes() == before, "new_copy overwrote original file"
    assert len(list((tmp_path / "out").rglob("*.md"))) == 2


def test_skip_is_not_reported_as_export_error(setup, tmp_path):
    settings, db = setup
    service_save(settings, db, tmp_path, "original text", intent="export")
    service_save(settings, db, tmp_path, "changed text", intent="export", policy="skip")


@pytest.fixture
def window(setup, tmp_path, monkeypatch):
    settings, db = setup
    import zhihu_collector.gui.main_window as mw
    from zhihu_collector.presets import PresetManager

    monkeypatch.setattr(mw, "load_settings", lambda: settings)
    monkeypatch.setattr(mw, "PresetManager", lambda: PresetManager(tmp_path / "presets.json"))
    monkeypatch.setattr(QMessageBox, "information", lambda *a: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "exec", lambda *a: QMessageBox.StandardButton.Close)
    app = QApplication.instance() or QApplication([])
    w = mw.MainWindow()
    yield app, w
    if w.worker and w.worker.isRunning():
        w.worker.wait(5000)
    w.worker = None
    w.close()
    app.processEvents()
    w.database.close()


def test_source_enqueue_rejects_active_duplicate(window):
    app, w = window
    req = CollectRequest(url="https://www.zhihu.com/people/audit", mode="answers", execution_intent="library")
    w._enqueue_requests([req])
    w._enqueue_requests([req])
    assert len(catalog.list_queue_items(w.database)) == 1


def test_main_window_fits_1280x720(window, tmp_path):
    app, w = window
    w.resize(1280, 720)
    w.show()
    app.processEvents()
    w._show_page(2)
    app.processEvents()
    size = (w.width(), w.height())
    hint = (w.minimumSizeHint().width(), w.minimumSizeHint().height())
    assert size[0] <= 1280 and size[1] <= 720, {"actual": size, "minimum_hint": hint}


def test_queue_advances_after_thread_finishes(window, monkeypatch):
    app, w = window
    import zhihu_collector.gui.main_window as mw
    import zhihu_collector.gui.worker as worker_module

    workers = []

    class FakeService:
        def __init__(self, settings, database, controller, **kw):
            self.db = database
            self.progress = type("P", (), {"message": "done"})()

        def run(self, req):
            tid = self.db.create_task(
                task_type="user_answers",
                target_url=req.url,
                target_id="audit",
                output_dir="",
                options=req.as_options(),
            )
            self.db.update_task(tid, status="completed")
            catalog.update_queue_item(self.db, req.queue_item_id, task_id=tid)
            return ""

    class DelayedExitWorker(worker_module.CollectWorker):
        def run(self):
            super().run()
            self.msleep(250)

    monkeypatch.setattr(worker_module, "CollectionService", FakeService)

    def make(*a, **kw):
        obj = DelayedExitWorker(*a, **kw)
        workers.append(obj)
        return obj

    monkeypatch.setattr(mw, "CollectWorker", make)
    w._enqueue_requests([
        CollectRequest(url=f"https://www.zhihu.com/people/audit{i}", mode="answers", execution_intent="library")
        for i in range(2)
    ])
    w._start_queue()
    until = time.monotonic() + 1.5
    while time.monotonic() < until:
        app.processEvents()
        time.sleep(0.01)
    for obj in workers:
        obj.wait(2000)
    app.processEvents()
    states = [r["status"] for r in catalog.list_queue_items(w.database)]
    assert states == ["success", "success"], states


def test_recover_scope_confirmation_after_restart(setup):
    settings, db = setup
    ids = catalog.enqueue_requests(
        db,
        [{"request_json": json.dumps({"url": "https://www.zhihu.com/collection/123", "mode": "collection"}), "dedupe_key": "collection:123|collection"}],
        batch_id="audit",
    )
    catalog.update_queue_item(db, ids[0], status="awaiting_confirm")
    db.recover_interrupted_tasks()
    catalog.reopen_queue_statuses(db, ("interrupted", "needs_login", "needs_verify"))
    row = catalog.claim_next_queue_item(db)
    assert row is not None, "awaiting_confirm remains stranded after restart and resume"


def test_start_queue_cannot_bypass_login_block(window, monkeypatch):
    app, w = window
    requests = [
        CollectRequest(url=f"https://www.zhihu.com/people/u{i}", mode="answers", execution_intent="library")
        for i in range(2)
    ]
    ids = w._enqueue_requests(requests)
    catalog.update_queue_item(w.database, ids[0], status="needs_login")
    w.queue.hold = True
    started = []
    monkeypatch.setattr(w, "_launch", lambda req: started.append(req.url))
    w._start_queue()
    assert started == [], started
