import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from zhihu_collector.browser.navigation import explain_failure, failure_diagnostic, navigation_once
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.listing import _goto
from zhihu_collector.config import Settings
from zhihu_collector.errors import LoginExpiredError, TaskStopped
from zhihu_collector.utils.retry import RetryGaveUp


@pytest.mark.parametrize("code, category, hint", [
    ("ERR_CONNECTION_CLOSED", "connection", "不能确定"),
    ("ERR_NAME_NOT_RESOLVED", "dns", "DNS"),
    ("ERR_PROXY_CONNECTION_FAILED", "proxy", "代理"),
    ("ERR_CERT_AUTHORITY_INVALID", "tls", "不会关闭证书校验"),
    ("ERR_INTERNET_DISCONNECTED", "network", "网络已断开"),
])
def test_diagnosis_distinguishes_failure_layer_without_exposing_credentials(monkeypatch, code, category, hint):
    from zhihu_collector.browser import navigation

    events = []
    monkeypatch.setattr(navigation, "logger", SimpleNamespace(warning=lambda _, row: events.append(row)))
    monkeypatch.setattr(navigation, "proxy_flags", lambda host: {"environment_proxy_configured": True})
    page = MagicMock()
    page.goto.side_effect = PlaywrightError(f"Page.goto: net::{code} https://user:password@example.com/path?token=secret#fragment")
    with pytest.raises(PlaywrightError) as caught:
        navigation_once(page, "https://user:password@example.com/path?token=secret#fragment")
    event = failure_diagnostic(caught.value)
    assert event["category"] == category
    assert event["host"] == "example.com" and event["path"] == "/path"
    assert hint in explain_failure(caught.value)
    serialized = json.dumps(event) + explain_failure(caught.value) + "".join(events)
    assert not any(secret in serialized for secret in ("password", "token=", "secret", "fragment", "user:"))


def test_retry_diagnostic_keeps_actual_attempt_count(monkeypatch):
    from zhihu_collector.browser import navigation

    monkeypatch.setattr(navigation, "proxy_flags", lambda _: {})
    page = MagicMock()
    page.url = "https://www.zhihu.com/collection/12"
    page.goto.side_effect = PlaywrightError("net::ERR_CONNECTION_CLOSED")
    with pytest.raises(RetryGaveUp) as caught:
        _goto(page, page.url, Settings(retry_count=2, retry_base_delay_ms=0), TaskController(), transient_only=True)
    assert page.goto.call_count == 2
    assert failure_diagnostic(caught.value)["attempt"] == 2
    assert "已尝试 2 次" in explain_failure(caught.value)


def test_timeout_is_distinct_from_disconnect():
    page = MagicMock()
    page.goto.side_effect = PlaywrightTimeout("navigation timeout")
    with pytest.raises(PlaywrightTimeout) as caught:
        navigation_once(page, "https://www.zhihu.com/collection/12")
    assert failure_diagnostic(caught.value)["category"] == "timeout"


def test_retry_wait_obeys_cancellation_before_another_attempt():
    controller = TaskController()
    page = MagicMock()
    page.url = "https://www.zhihu.com/collection/12"

    def disconnect(*args, **kwargs):
        controller.stop()
        raise PlaywrightError("net::ERR_CONNECTION_CLOSED")

    page.goto.side_effect = disconnect
    with pytest.raises(TaskStopped):
        _goto(page, page.url, Settings(retry_count=3, retry_base_delay_ms=1000), controller, transient_only=True)
    assert page.goto.call_count == 1


def test_authentication_signal_is_not_reclassified_as_connection_failure():
    page = MagicMock()
    page.url = "https://www.zhihu.com/signin"
    page.goto.side_effect = PlaywrightTimeout("timeout after login redirect")
    with pytest.raises(LoginExpiredError):
        _goto(page, page.url, Settings(retry_count=2), TaskController(), transient_only=True)
    assert page.goto.call_count == 1


@pytest.mark.parametrize("diagnostic_writable", [True, False])
def test_task_failure_persists_actionable_message_even_if_diagnostic_disk_fails(tmp_path, monkeypatch, diagnostic_writable):
    from zhihu_collector.collectors import service as module
    from zhihu_collector.database.database import Database
    from zhihu_collector.errors import CollectorError

    monkeypatch.setattr(module, "log_dir", lambda: tmp_path / "logs")
    if not diagnostic_writable:
        def unwritable(*args, **kwargs):
            raise PermissionError("diagnostic disk unwritable")
        monkeypatch.setattr(module, "append_jsonl", unwritable)
    browser = MagicMock()
    browser.__enter__.return_value = browser
    monkeypatch.setattr(module, "BrowserSession", lambda **kw: browser)
    page = MagicMock()
    page.goto.side_effect = PlaywrightError("net::ERR_CONNECTION_CLOSED https://www.zhihu.com/?token=secret")

    def fail(*args, **kwargs):
        navigation_once(page, "https://www.zhihu.com/question/9/answer/31?token=secret")

    monkeypatch.setattr(module.CollectionService, "_collect_single_answer", fail)
    db = Database(tmp_path / "sandbox.db")
    try:
        worker = module.CollectionService(Settings(), db, TaskController())
        with pytest.raises(CollectorError, match="连接被关闭"):
            worker.run(module.CollectRequest(url="https://www.zhihu.com/question/9/answer/31",
                                            execution_intent="library", download_images=False))
        task = db.list_tasks()[0]
        assert task["status"] == "failed"
        assert "ERR_CONNECTION_CLOSED" in task["error_message"]
        assert "secret" not in task["error_message"]
        assert "连接被关闭" in db.list_failures()[0]["reason"]
        if diagnostic_writable:
            rows = list((tmp_path / "logs" / "connection_diagnostics").glob("*.jsonl"))
            assert len(rows) == 1
            event = json.loads(rows[0].read_text(encoding="utf-8"))
            assert event["category"] == "connection" and event["attempt"] == 1
    finally:
        db.close()
