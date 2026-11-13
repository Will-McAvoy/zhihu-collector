from pathlib import Path
from unittest.mock import MagicMock
import pytest
from playwright.sync_api import Error as PlaywrightError
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.service import CollectionService, CollectRequest
from zhihu_collector.config import Settings
from zhihu_collector.models import ContentRecord
from zhihu_collector.utils.urls import parse_zhihu_url
import zhihu_collector.collectors.service as service_module

URL = 'https://www.zhihu.com/question/100/answer/12'
NAVIGATION = 'Page.content: Unable to retrieve content because the page is navigating and changing the content.'

@pytest.fixture
def sample(monkeypatch):
    controller = TaskController()
    service = CollectionService(Settings(request_interval_ms=1), MagicMock(), controller)
    service.task_id = 1
    page = MagicMock()
    page.url = URL
    session = MagicMock()
    session.new_page.return_value = page
    complete = ContentRecord('12', 'answer', URL, html_content='<p>Complete navigation regression answer body.</p>')
    monkeypatch.setattr(service_module, 'records_from_page_sources', lambda **kwargs: [complete])
    return service, page, session


def test_enrichment_retries_document_changed_during_navigation(sample):
    service, page, session = sample
    page.content.side_effect = [PlaywrightError(NAVIGATION), '<html>stable</html>']
    record = ContentRecord('12', 'answer', URL, content_incomplete=True)
    service._enrich(session, record)
    assert not record.content_incomplete
    assert 'Complete navigation' in record.html_content
    assert page.content.call_count == 2
    assert page.goto.call_count == 1


def test_single_answer_retries_document_changed_during_navigation(sample, tmp_path, monkeypatch):
    service, page, session = sample
    page.content.side_effect = [PlaywrightError(NAVIGATION), '<html>stable</html>']
    persist = MagicMock()
    monkeypatch.setattr(service, '_persist_record', persist)
    service._collect_single_answer(session, parse_zhihu_url(URL), CollectRequest(url=URL), tmp_path, None, None)
    persist.assert_called_once()
    assert page.content.call_count == 2
    page.close.assert_called_once()


@pytest.mark.parametrize("destination,error", [
    ("https://www.zhihu.com/signin", "LoginExpiredError"),
    ("https://www.zhihu.com/account/unhuman?type=captcha", "VerificationRequiredError"),
])
def test_navigation_to_auth_page_stops_retry(sample, destination, error):
    from zhihu_collector.browser.page_content import read_page_content
    from zhihu_collector import errors
    service, page, session = sample
    page.content.side_effect = PlaywrightError(NAVIGATION)
    page.wait_for_load_state.side_effect = lambda *a, **kw: setattr(page, "url", destination)
    with pytest.raises(getattr(errors, error)):
        read_page_content(page, service.controller)
    assert page.content.call_count == 1


def test_cancel_during_navigation_retry_stops_before_next_read(sample):
    from zhihu_collector.browser.page_content import read_page_content
    from zhihu_collector.errors import TaskStopped
    service, page, session = sample
    page.content.side_effect = PlaywrightError(NAVIGATION)
    page.wait_for_load_state.side_effect = lambda *a, **kw: service.controller.stop()
    with pytest.raises(TaskStopped):
        read_page_content(page, service.controller)
    assert page.content.call_count == 1


def test_permanent_page_error_is_not_retried(sample):
    from zhihu_collector.browser.page_content import read_page_content
    service, page, session = sample
    page.content.side_effect = PlaywrightError("Target page, context or browser has been closed")
    with pytest.raises(PlaywrightError, match="closed"):
        read_page_content(page, service.controller)
    page.content.assert_called_once()
    page.wait_for_load_state.assert_not_called()


def test_continuous_navigation_has_attempt_limit(sample):
    from zhihu_collector.browser.page_content import read_page_content
    from zhihu_collector.errors import CollectorError
    service, page, session = sample
    page.content.side_effect = PlaywrightError(NAVIGATION)
    with pytest.raises(CollectorError, match="页面持续跳转") as failure:
        read_page_content(page, service.controller, attempts=3)
    assert page.content.call_count == 3
    assert isinstance(failure.value.__cause__, PlaywrightError)
    page.goto.assert_not_called()


def test_retry_time_budget_is_respected(sample, monkeypatch):
    from types import SimpleNamespace
    from zhihu_collector.browser import page_content
    from zhihu_collector.errors import CollectorError
    service, page, session = sample
    clock = [0.0]
    monkeypatch.setattr(page_content, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    page.content.side_effect = PlaywrightError(NAVIGATION)
    page.wait_for_load_state.side_effect = lambda *a, **kw: clock.__setitem__(0, 2.0)
    with pytest.raises(CollectorError):
        page_content.read_page_content(page, service.controller, timeout_ms=1000)
    assert page.content.call_count == 1


def test_dom_ready_timeout_does_not_discard_retry(sample):
    from zhihu_collector.browser.page_content import read_page_content
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    service, page, session = sample
    page.content.side_effect = [PlaywrightError(NAVIGATION), "<html>stable</html>"]
    page.wait_for_load_state.side_effect = PlaywrightTimeout("DOM load timeout")
    assert read_page_content(page, service.controller) == "<html>stable</html>"


def test_enrichment_waits_for_exact_answer_after_initial_empty_document(sample, monkeypatch):
    service, page, session = sample
    page.content.side_effect = ["<html>loading</html>", "<html>ready</html>"]
    complete = ContentRecord("12", "answer", URL, html_content="<p>Actual target answer.</p>")
    monkeypatch.setattr(service_module, "records_from_page_sources", lambda **kw: [complete] if "ready" in kw["html"] else [])
    record = ContentRecord("12", "answer", URL, content_incomplete=True)
    service._enrich(session, record)
    assert not record.content_incomplete
    assert record.html_content == complete.html_content
    assert page.content.call_count == 2


def test_unrelated_answer_does_not_satisfy_target_readiness(sample, monkeypatch):
    service, page, session = sample
    other = ContentRecord("123", "answer", URL, html_content="<p>Other answer.</p>")
    target = ContentRecord("12", "answer", URL, html_content="<p>Correct answer.</p>")
    page.content.side_effect = ["<html>other</html>", "<html>target</html>"]
    monkeypatch.setattr(service_module, "records_from_page_sources", lambda **kw: [target] if "target" in kw["html"] else [other])
    record = ContentRecord("12", "answer", URL, content_incomplete=True)
    service._enrich(session, record)
    assert record.html_content == target.html_content
    assert page.content.call_count == 2


def test_missing_answer_readiness_has_attempt_limit(sample):
    from zhihu_collector.browser.page_content import read_page_content
    from zhihu_collector.errors import CollectorError
    service, page, session = sample
    page.content.return_value = "<html>loading</html>"
    with pytest.raises(CollectorError, match="正文尚未加载"):
        read_page_content(page, service.controller, is_ready=lambda html: False, attempts=3)
    assert page.content.call_count == 3


@pytest.mark.parametrize("body", [
    "<p>这是正常的叙述……</p><p>后面还有完整正文。</p>",
    "<p>这是完整回答自己的结尾……</p>",
    "<p>作者自己的停顿... </p><p>还有下一段。</p>",
])
def test_collection_does_not_mistake_author_ellipsis_for_excerpt(body):
    from zhihu_collector.parsers.collection_parser import answer_body_needs_enrichment
    assert answer_body_needs_enrichment(body) is False


@pytest.mark.parametrize("body", [
    '<div class="is-collapsed"><p>尚未展开的摘要</p></div>',
    '<p>摘要</p><button class="ContentItem-more">更多</button>',
])
def test_collection_still_rejects_explicit_collapsed_body(body):
    from zhihu_collector.parsers.collection_parser import answer_body_needs_enrichment
    assert answer_body_needs_enrichment(body) is True
