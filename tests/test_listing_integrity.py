"""作者列表的分页归属、结束差额和诊断记录。"""

from unittest.mock import MagicMock

import zhihu_collector.collectors.service as svc
from zhihu_collector.browser.network_listener import CapturedResponse
from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.listing import _page_says_no_more
from zhihu_collector.collectors.base import Progress, progress_labels
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.collectors.diagnostics import append_jsonl
from zhihu_collector.collectors.listing import region_texts_say_no_more, select_records_to_emit
from zhihu_collector.collectors.paging import ScrollState
from zhihu_collector.collectors.service import classify_completion
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.utils.list_target import (
    ListTarget,
    apply_bound_paging,
    closure_notice,
    describe_paging,
    response_matches_list,
    sanitize_endpoint,
    success_summary,
    summarize_list_items,
)


AUTHOR = ListTarget("member_answers", "example-22")
QUESTION = ListTarget("question_answers", "12")


def _response(url: str, paging: dict, rows=None, status: int = 200) -> CapturedResponse:
    data = {"paging": paging, "data": rows or []}
    return CapturedResponse(url=url, status=status, data=data, paging=describe_paging(data))


def _record(content_id: str) -> ContentRecord:
    return ContentRecord(
        zhihu_content_id=content_id,
        content_type="answer",
        url=f"https://www.zhihu.com/question/1/answer/{content_id}",
        title=content_id,
        author=AuthorInfo(name="测试作者", url_token="example-22"),
        html_content=f"<p>{content_id}</p>",
    )


def test_author_list_ignores_foreign_feed_end_and_totals():
    author = _response(
        "https://www.zhihu.com/api/v4/members/example-22/answers?offset=40&access_token=secret",
        {"is_end": False, "totals": 54, "next": "https://www.zhihu.com/api/v4/members/example-22/answers?offset=60&access_token=secret"},
    )
    feed = _response(
        "https://www.zhihu.com/api/v4/questions/99/feeds",
        {"is_end": True, "totals": 999, "next": ""},
    )
    state = ScrollState()
    decision = apply_bound_paging(state, [author, feed], AUTHOR)
    assert decision["is_end"] is False
    assert decision["has_next"] is True
    assert decision["totals"] == 54
    assert "access_token" not in (decision["held"]["url"] or "")
    assert "access_token" not in (decision["held"]["next"] or "")
    state.observe_page(["1"], is_end=decision["is_end"], has_next=decision["has_next"])
    assert state.reached_end is False

    later = apply_bound_paging(state, [feed], AUTHOR)
    assert later["has_next"] is True
    assert later["totals"] == 54
    assert state.declared_total == 54


def test_similar_identity_and_detail_urls_do_not_match():
    assert response_matches_list("https://www.zhihu.com/api/v4/members/example-22/answers", AUTHOR)
    assert not response_matches_list("https://www.zhihu.com/api/v4/members/example-2/answers", AUTHOR)
    assert not response_matches_list("https://www.zhihu.com/api/v4/members/example-220/answers", AUTHOR)
    assert not response_matches_list("https://www.zhihu.com/api/v4/members/example-22/answers/100", AUTHOR)
    assert not response_matches_list("https://www.zhihu.com/api/v4/questions/12/feeds", AUTHOR)
    assert response_matches_list("https://www.zhihu.com/api/v4/questions/12/answers", QUESTION)
    assert response_matches_list("https://www.zhihu.com/api/v4/questions/12/feeds", QUESTION)
    assert not response_matches_list("https://www.zhihu.com/api/v4/questions/123/feeds", QUESTION)
    assert not response_matches_list("https://www.zhihu.com/api/v4/questions/12/answers/9", QUESTION)
    assert not response_matches_list("https://www.zhihu.com/api/v4/feeds", QUESTION)


def test_missing_next_without_is_end_does_not_finish():
    item = _response("https://www.zhihu.com/api/v4/members/example-22/answers", {"totals": 54})
    state = ScrollState()
    decision = apply_bound_paging(state, [item], AUTHOR)
    assert decision["is_end"] is False
    assert decision["has_next"] is None
    state.observe_page(["1"], is_end=decision["is_end"], has_next=decision["has_next"])
    assert state.reached_end is False
    assert state.end_evidence == ""


def test_explicit_empty_next_is_distinct_from_missing_next():
    item = _response(
        "https://www.zhihu.com/api/v4/members/example-22/answers",
        {"is_end": False, "totals": 49, "next": ""},
    )
    state = ScrollState()
    decision = apply_bound_paging(state, [item], AUTHOR)
    state.observe_page(["1"], is_end=decision["is_end"], has_next=decision["has_next"])
    assert state.end_evidence == "no_next"
    assert "没有下一页" in state.stop_reason
    assert state.stop_reason != "分页已经结束：接口声明已到末页"


def test_overlap_counts_duplicates_not_discovered_items():
    state = ScrollState()
    first = [_record(str(i)) for i in range(1, 50)]
    select_records_to_emit(first, state=state, known_versions={}, has_next=True)
    overlap = [_record(str(i)) for i in range(45, 50)]
    select_records_to_emit(overlap, state=state, known_versions={}, has_next=True)
    assert len(state.seen_ids) == 49
    assert state.duplicate_count == 5


def test_declared_total_change_is_recorded_from_the_same_target():
    state = ScrollState()
    first = _response("https://www.zhihu.com/api/v4/members/example-22/answers", {"is_end": False, "totals": 54, "next": "https://www.zhihu.com/api/v4/members/example-22/answers?offset=20"})
    second = _response("https://www.zhihu.com/api/v4/members/example-22/answers?offset=20", {"is_end": True, "totals": 55, "next": ""})
    apply_bound_paging(state, [first], AUTHOR)
    apply_bound_paging(state, [second], AUTHOR)
    assert state.totals_seen == [54, 55]
    assert state.declared_total == 55


def test_gap_with_reliable_end_is_not_called_five_failures():
    notice = closure_notice(
        saved_total=49,
        saved_new=49,
        saved_updated=0,
        reused=0,
        scanned=49,
        declared=54,
        end_evidence="is_end",
        totals_seen=[54],
    )
    assert "保存49篇" in notice
    assert "列表遍历完成" in notice
    assert "相差5条" in notice
    assert "尚未核实" in notice
    assert "无法确认全量完整" in notice
    assert "搜集完成" not in notice
    assert "失败" not in notice
    assert classify_completion(notice) == "unconfirmed"


def test_weak_end_does_not_claim_the_list_was_fully_traversed():
    notice = closure_notice(
        saved_total=49,
        saved_new=49,
        saved_updated=0,
        reused=0,
        scanned=49,
        declared=54,
        end_evidence="stale",
    )
    assert "差额5条尚未核实" in notice
    assert "列表遍历完成" not in notice
    assert "失败" not in notice


def test_matching_counts_and_real_end_can_say_complete():
    assert closure_notice(
        saved_total=49,
        saved_new=49,
        saved_updated=0,
        reused=0,
        scanned=49,
        declared=49,
        end_evidence="is_end",
    ) == ""
    assert "新增 29" in success_summary(29, 0, 20)
    assert "跳过 20" in success_summary(29, 0, 20)


def test_unparsed_items_are_counted_and_block_a_complete_claim():
    rows = [{"type": "answer", "id": i} for i in range(1, 5)]
    rows.extend([{"type": "answer"}, {"foo": 1}])
    item = _response("https://www.zhihu.com/api/v4/members/example-22/answers", {"is_end": True, "totals": 6, "next": ""}, rows)
    summary = summarize_list_items([item], AUTHOR)
    assert summary["unparsed"] == 2
    notice = closure_notice(
        saved_total=4,
        saved_new=4,
        saved_updated=0,
        reused=0,
        scanned=4,
        declared=6,
        end_evidence="is_end",
        unparsed=2,
    )
    assert "未能解析" in notice
    assert "列表遍历完成" not in notice
    assert "失败2" not in notice


def test_limit_stop_is_explained_without_an_unconfirmed_gap():
    assert closure_notice(
        saved_total=2,
        saved_new=2,
        saved_updated=0,
        reused=0,
        scanned=2,
        declared=54,
        end_evidence="stale",
        intentional="limit",
    ) == ""


def test_page_end_text_comes_only_from_the_list_region():
    assert region_texts_say_no_more(["列表没有更多了"]) is True
    assert region_texts_say_no_more(["暂无内容"]) is False
    assert region_texts_say_no_more([]) is False

    class WholePage:
        def inner_text(self, selector):
            return "侧边栏没有更多了 暂无内容"

        def evaluate(self, script, selectors):
            return []

    assert _page_says_no_more(WholePage()) is False


def test_progress_labels_keep_reference_total_separate():
    progress = Progress(found=49, completed=49, scanned=49, declared_total=54, saved_new=49)
    found, done = progress_labels(progress)
    assert found == "已扫描：49篇，网站参考54条"
    assert "本次新增：49" in done
    assert classify_completion("搜集结束，但有失败项：1 条内容处理失败") == "partial"


def test_diagnostic_line_drops_credentials_and_trims(tmp_path):
    path = append_jsonl(
        tmp_path,
        7,
        {
            "endpoint": sanitize_endpoint("https://www.zhihu.com/api/v4/members/example-22/answers?offset=20&access_token=secret"),
            "cookie": "z_c0=secret",
            "note": "Bearer secret",
        },
        max_lines=20,
    )
    text = path.read_text(encoding="utf-8")
    assert "offset=20" in text
    assert "access_token" not in text
    assert "secret" not in text
    assert "z_c0" not in text
    append_jsonl(tmp_path, 7, {"kind": "page", "page_index": 2}, max_lines=2)
    append_jsonl(tmp_path, 7, {"kind": "summary", "page_index": 3}, max_lines=2)
    trimmed = path.read_text(encoding="utf-8")
    assert trimmed.count("\n") == 2
    assert "page_index" in trimmed


def test_service_reports_unconfirmed_gap_without_calling_it_complete(tmp_path, monkeypatch):
    database = Database(tmp_path / "zhihu.db")
    service = svc.CollectionService(Settings(), database, TaskController())
    monkeypatch.setattr(svc, "BrowserSession", MagicMock())
    monkeypatch.setattr(svc, "log_dir", lambda: tmp_path / "logs")

    def listing(*args, **kwargs):
        state = ScrollState()
        state.seen_ids = {f"a{i}" for i in range(49)}
        state.declared_total = 54
        state.totals_seen = [54]
        state.end_evidence = "is_end"
        state.reached_end = True
        kwargs["state_box"]["state"] = state
        kwargs["on_page"]([_record("1")], 54, 49)

    monkeypatch.setattr(svc, "collect_listing", listing)
    service.run(svc.CollectRequest(
        url="https://www.zhihu.com/people/example-22",
        mode="answers",
        output_dir=str(tmp_path / "out"),
        download_images=False,
        export_docx=False,
        export_pdf=False,
    ))
    task = database.latest_task("https://www.zhihu.com/people/example-22")
    assert task["status"] == "unconfirmed"
    assert "相差5条" in task["error_message"]
    assert "失败" not in (task["error_message"] or "")
    assert "搜集完成" not in (service.progress.message or "")
    assert service.progress.scanned == 49
    assert service.progress.declared_total == 54
    assert service.progress.percent is not None and service.progress.percent < 100
    database.close()
