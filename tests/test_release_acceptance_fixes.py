"""Regressions from the final 0.1.3 acceptance run."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from zhihu_collector.browser.network_listener import CapturedResponse
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.config import Settings


def _run_collection(monkeypatch, pages, *, navigation=None, redirected=None):
    from zhihu_collector.collectors import collection_answers as module

    page = MagicMock()
    page.url = redirected or "https://www.zhihu.com/collection/12"
    page.content.return_value = "<html><title>测试</title><body>收藏夹</body></html>"
    page.inner_text.return_value = "收藏夹"
    page.goto.side_effect = navigation
    browser = MagicMock()
    browser.new_page.return_value = page
    listener = MagicMock()
    listener.snapshot.return_value = []
    listener.consume_errors.return_value = []
    listener.consume.side_effect = [
        [CapturedResponse(f"https://www.zhihu.com/api/v4/collections/12/items?offset={i}", 200, payload)]
        for i, payload in enumerate(pages)
    ] + [[]] * 6
    monkeypatch.setattr(module, "NetworkListener", lambda: listener)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    def run():
        return module.collect_collection_manifest(
            browser, "12", Settings(max_stale_rounds=3, retry_count=2, retry_base_delay_ms=0), TaskController(),
        )

    return page, run


def _page(answer_id, total, end):
    return {
        "data": [{"content": {"id": answer_id, "type": "answer", "content": "<p>正文</p>",
                              "question": {"id": "2", "title": "标题"}}}],
        "paging": {"is_end": end, "totals": total, "next": "/api/v4/collections/12/items?offset=20"},
    }


@pytest.mark.parametrize("declared, expected", [(2, "complete"), (3, "partial")])
def test_collection_terminal_marker_wins_even_when_cursor_repeats(monkeypatch, declared, expected):
    page, run = _run_collection(monkeypatch, [_page("1", declared, False), _page("2", declared, True)])
    result = run()
    assert result.integrity_status == expected
    assert result.pages_traversed == 2
    assert [row.zhihu_content_id for row in result.answers] == ["1", "2"]
    assert page.evaluate.call_count == 1
    assert "完整性未确认" in result.finish_reason if declared == 3 else "已到达收藏夹末尾" in result.finish_reason


def test_collection_navigation_retries_transient_disconnect(monkeypatch):
    page, run = _run_collection(
        monkeypatch, [_page("1", 1, True)],
        navigation=[PlaywrightError("Page.goto: net::ERR_CONNECTION_CLOSED"), None],
    )
    assert run().integrity_status == "complete"
    assert page.goto.call_count == 2


def test_collection_navigation_does_not_retry_a_permanent_error(monkeypatch):
    page, run = _run_collection(monkeypatch, [], navigation=[PlaywrightError("net::ERR_INVALID_URL"), None])
    with pytest.raises(PlaywrightError, match="ERR_INVALID_URL"):
        run()
    assert page.goto.call_count == 1


def test_collection_timeout_on_signin_is_not_retried(monkeypatch):
    page, run = _run_collection(monkeypatch, [], navigation=[PlaywrightTimeout("timeout")],
                                redirected="https://www.zhihu.com/signin")
    assert run().interrupted == "login"
    assert page.goto.call_count == 1


def test_pin_empty_html_falls_back_to_safe_link_card():
    from bs4 import BeautifulSoup
    from zhihu_collector.parsers.pin_parser import parse_pin_object

    record = parse_pin_object({
        "id": "12", "type": "pin", "content_html": "<div></div>",
        "content": [
            {"type": "text", "content": ""},
            {"type": "link_card", "data_draft_title": "链接标题 <示例>",
             "url": "https://www.zhihu.com/question/2/answer/3"},
            {"type": "link_card", "data_draft_title": "unsafe", "url": "javascript:alert(1)"},
        ],
    })
    assert not record.content_incomplete
    links = BeautifulSoup(record.html_content, "lxml").find_all("a")
    assert len(links) == 1 and links[0]["href"] == "https://www.zhihu.com/question/2/answer/3"
    assert links[0].get_text() == "链接标题 <示例>"


@pytest.mark.parametrize("good_answer, expected", [(True, "partial"), (False, "failed")])
def test_unusable_pin_records_retryable_failure_and_correct_task_status(tmp_path, monkeypatch, good_answer, expected):
    from zhihu_collector.collectors import service as module
    from zhihu_collector.collectors.queue_session import QueueSession
    from zhihu_collector.database import catalog
    from zhihu_collector.database.database import Database
    from zhihu_collector.gui.main_window import MainWindow
    from zhihu_collector.models import AuthorInfo, ContentRecord

    db = Database(tmp_path / "task.db")
    author = AuthorInfo(name="测试", url_token="sample", url="https://www.zhihu.com/people/sample")
    answer = ContentRecord(zhihu_content_id="31", content_type="answer", author=author,
                           title="正常", url="https://www.zhihu.com/question/9/answer/31", html_content="<p>正常正文</p>")
    pin = ContentRecord(zhihu_content_id="32", content_type="pin", author=author,
                        url="https://www.zhihu.com/pin/32", html_content="<div></div>")
    browser = MagicMock()
    browser.__enter__.return_value = browser
    monkeypatch.setattr(module, "BrowserSession", lambda **kw: browser)
    monkeypatch.setattr(module.CollectionService, "_enrich", lambda *args: None)

    def collect(_session, _url, **kwargs):
        is_pin = kwargs["list_target"].kind == "member_pins"
        rows = [pin] if is_pin else [answer] if good_answer else []
        kwargs["on_page"](rows, len(rows), len(rows))

    monkeypatch.setattr(module, "collect_listing", collect)
    try:
        item_id = catalog.enqueue_requests(
            db, [{"dedupe_key": "user:sample", "target_url": author.url, "request_json": "{}"}],
            batch_id="acceptance",
        )[0]
        catalog.update_queue_item(db, item_id, status="running")
        service = module.CollectionService(Settings(), db, TaskController())
        service.run(module.CollectRequest(
            url=author.url, mode="all", output_dir=str(tmp_path / "output"),
            execution_intent="library", download_images=False, queue_item_id=item_id,
        ))
        task = db.list_tasks()[0]
        failures = db.list_failures()
        assert task["status"] == expected
        assert module.classify_completion(service.progress.message) == expected
        assert task["total_completed"] == int(good_answer)
        assert [f["zhihu_content_id"] for f in failures if f["kind"] == "body"] == ["32"]
        view = SimpleNamespace(_current_queue_id=item_id, database=db, queue=QueueSession(db))
        assert MainWindow._finish_current_from_task(view) == expected
        queue_item = catalog.list_queue_items(db)[0]
        assert queue_item["status"] == expected
    finally:
        db.close()


def test_browser_restarts_driver_with_local_temp_and_restores_environment(tmp_path, monkeypatch):
    from zhihu_collector.browser import playwright_manager as module

    failing = MagicMock()
    failing.chromium.launch.side_effect = PlaywrightError(
        "BrowserType.launch: EPERM: operation not permitted, mkdtemp 'playwright-artifacts-XXXXXX'"
    )
    working = MagicMock()
    calls = []
    original = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR")}

    def start():
        calls.append(os.environ.get("TEMP"))
        return failing if len(calls) == 1 else working

    monkeypatch.setattr(module, "_default_browser_temp_is_writable", lambda: True)
    monkeypatch.setattr(module, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(module, "sync_playwright", lambda: type("Manager", (), {"start": staticmethod(start)})())
    with module.BrowserSession() as session:
        assert session.browser is working.chromium.launch.return_value
        assert Path(calls[0]).parent == tmp_path / "browser-temp"
        assert calls[0] != calls[1]
        assert Path(calls[1]).parent == tmp_path / "browser-temp"
        assert {k: os.environ.get(k) for k in original} == original
    failing.stop.assert_called_once()
    working.stop.assert_called_once()
    from zhihu_collector.browser.owned_temp import wait_for_cleanup
    assert wait_for_cleanup(4)
    assert list((tmp_path / "browser-temp").iterdir()) == []
