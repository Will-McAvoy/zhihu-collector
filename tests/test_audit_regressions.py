from zhihu_collector.browser.page_guard import inspect_page
from zhihu_collector.collectors.listing import _version, select_records_to_emit
from zhihu_collector.collectors.paging import ScrollState
from zhihu_collector.collectors.service import (
    CollectRequest,
    export_saved_rows,
    retry_failed_images,
    write_separate_exports,
)
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.downloader.image_downloader import ImageAsset
from zhihu_collector.errors import LoginExpiredError
from zhihu_collector.models import AuthorInfo, ContentRecord


class FakePage:
    def __init__(self, url: str, text: str = ""):
        self.url = url
        self._text = text

    def inner_text(self, selector: str) -> str:
        return self._text


def _author() -> AuthorInfo:
    return AuthorInfo(
        zhihu_user_id="u1",
        name="张三",
        url_token="zhangsan",
        url="https://www.zhihu.com/people/zhangsan",
    )


def _record(content_id: str, html: str, *, url: str = "", question_id: str = "2", title: str = "同样的标题") -> ContentRecord:
    return ContentRecord(
        zhihu_content_id=content_id,
        content_type="answer",
        url=url or (f"https://www.zhihu.com/question/{question_id}/answer/{content_id}" if content_id else ""),
        title=title,
        author=_author(),
        question_id=question_id,
        question_title=title,
        question_url=f"https://www.zhihu.com/question/{question_id}",
        html_content=html,
        updated_time=1700001000,
    )


def _save(database: Database, record: ContentRecord, *, index_scope: str = "author"):
    return database.save_content(record, markdown=record.html_content, html=record.html_content, index_scope=index_scope)


def test_same_body_with_different_ids_stays_two_rows(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    html = "<p>完全相同的正文</p>"
    assert _save(database, _record("1001", html, question_id="2"))[0] == "inserted"
    assert _save(database, _record("1002", html, question_id="3"))[0] == "inserted"
    assert database.count_contents() == 2
    again, _ = _save(database, _record("1001", html, question_id="2"))
    assert again == "skipped"
    changed = _record("1001", "<p>修改后的正文</p>", question_id="2")
    changed.updated_time = 1700002000
    assert _save(database, changed)[0] == "updated"
    assert database.count_contents() == 2


def test_missing_id_uses_url_then_can_be_upgraded(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    html = "<p>没有 ID 的正文</p>"
    first = _record("", html, url="https://www.zhihu.com/question/9/answer/temp")
    assert _save(database, first)[0] == "inserted"
    assert _save(database, first)[0] == "skipped"
    assert database.count_contents() == 1
    upgraded = _record("88", html, url="https://www.zhihu.com/question/9/answer/temp")
    assert _save(database, upgraded)[0] == "skipped"
    stored = database.list_contents()
    assert len(stored) == 1
    assert stored[0]["zhihu_content_id"] == "88"
    hash_only = ContentRecord(
        zhihu_content_id="",
        content_type="answer",
        url="",
        html_content=html,
        author=_author(),
    )
    assert _save(database, hash_only)[0] == "inserted"
    assert database.count_contents() == 2


def test_resume_walks_past_unchanged_pages_until_new_content():
    state = ScrollState(max_stale_rounds=5)
    known = {}
    for index in range(6):
        record = _record(str(index), "<p>旧正文</p>", question_id=str(index))
        known[str(index)] = _version(record)
        emitted = select_records_to_emit(
            [record],
            state=state,
            known_versions=known,
            has_next=True,
        )
        assert emitted == []
        assert state.reached_end is False
    fresh = _record("77", "<p>新正文</p>", question_id="77")
    emitted = select_records_to_emit([fresh], state=state, known_versions=known, has_next=True)
    assert [item.zhihu_content_id for item in emitted] == ["77"]
    assert state.reached_end is False


def test_repeated_page_stops_but_incremental_stop_requires_time_order():
    state = ScrollState(max_stale_rounds=3)
    record = _record("a", "<p>同一页</p>")
    select_records_to_emit([record], state=state, known_versions={}, has_next=True)
    for _ in range(3):
        select_records_to_emit([record], state=state, known_versions={}, has_next=True)
    assert state.reached_end is True

    old = _record("a", "<p>旧</p>")
    known = {"a": _version(old)}
    incremental = ScrollState()
    select_records_to_emit(
        [old],
        state=incremental,
        known_versions=known,
        update_only=True,
        sorted_by_time=True,
        has_next=True,
    )
    assert incremental.reached_end is True
    assert "增量" in incremental.stop_reason

    scanning = ScrollState(max_stale_rounds=5)
    select_records_to_emit(
        [old],
        state=scanning,
        known_versions=known,
        update_only=True,
        sorted_by_time=False,
        has_next=True,
    )
    assert scanning.reached_end is False
    changed = _record("a", "<p>有更新</p>")
    changed.updated_time = 1700003000
    emit = select_records_to_emit(
        [changed],
        state=ScrollState(),
        known_versions=known,
        update_only=True,
        sorted_by_time=True,
        has_next=True,
    )
    assert [item.zhihu_content_id for item in emit] == ["a"]


def test_failed_export_can_be_retried_and_new_format_is_added(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    _, local_id = _save(database, _record("5", "<p>正文</p>"))
    row = database.get_content(local_id)
    directory = tmp_path / "回答"
    settings = Settings()

    from test_export_regressions import MarkerPdf

    class FlakyPdf(MarkerPdf):
        def __init__(self):
            self.calls = 0

        def write(self, markdown_text, destination, *, title):
            self.calls += 1
            if self.calls == 1:
                raise OSError("pdf failed")
            return super().write(markdown_text, destination, title=title)

    pdf = FlakyPdf()
    first = CollectRequest(url="https://www.zhihu.com/people/zhangsan", export_docx=False, export_pdf=True)
    results = write_separate_exports(row, [], directory, first, settings, pdf, database)
    assert results["markdown"] == "success"
    assert results["pdf"] != "success"
    assert export_saved_rows(database, [database.get_content(local_id)], tmp_path, first, settings, pdf) == 0
    assert list(directory.glob("*.pdf"))

    markdown_only = CollectRequest(
        url="https://www.zhihu.com/people/zhangsan",
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
    )
    write_separate_exports(row, [], directory, markdown_only, settings, None, database)
    with_docx = CollectRequest(
        url="https://www.zhihu.com/people/zhangsan",
        export_markdown=True,
        export_docx=True,
        export_pdf=False,
    )
    assert export_saved_rows(database, [database.get_content(local_id)], tmp_path, with_docx, settings) == 0
    assert list(directory.glob("*.docx"))

    other = tmp_path / "moved"
    assert export_saved_rows(database, [database.get_content(local_id)], other, with_docx, settings) == 0
    assert list((other / "回答").glob("*.md"))
    assert list((other / "回答").glob("*.docx"))


def test_same_title_does_not_overwrite_and_update_reuses_path(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    settings = Settings(filename_rule="{title}")
    request = CollectRequest(
        url="https://www.zhihu.com/people/zhangsan",
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
        conflict_policy="overwrite",
    )
    long_title = "超长标题" * 30
    _, first_id = _save(database, _record("11", "<p>甲</p>", question_id="1", title=long_title))
    _, second_id = _save(database, _record("22", "<p>乙</p>", question_id="2", title=long_title))
    directory = tmp_path / "回答"
    write_separate_exports(database.get_content(first_id), [], directory, request, settings, None, database)
    write_separate_exports(database.get_content(second_id), [], directory, request, settings, None, database)
    files = sorted(directory.glob("*.md"))
    assert len(files) == 2
    texts = {path.read_text(encoding="utf-8") for path in files}
    assert any("甲" in text for text in texts)
    assert any("乙" in text for text in texts)
    owned = database.preferred_markdown_path(first_id)
    changed = _record("11", "<p>甲的更新</p>", question_id="1", title=long_title)
    changed.updated_time = 1700002222
    _save(database, changed)
    write_separate_exports(database.get_content(first_id), [], directory, request, settings, None, database)
    assert sorted(directory.glob("*.md")) == files
    assert database.preferred_markdown_path(first_id) == owned
    assert "甲的更新" in next(path for path in files if path.read_text(encoding="utf-8").find("甲的更新") >= 0).read_text(encoding="utf-8")


def test_author_export_index_does_not_restart_for_each_question(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    _, first_id = _save(database, _record("1", "<p>一</p>", question_id="10"))
    _, second_id = _save(database, _record("2", "<p>二</p>", question_id="20"))
    assert database.get_content(first_id)["export_index"] == 1
    assert database.get_content(second_id)["export_index"] == 2


def test_login_redirect_is_not_hidden_by_old_content():
    try:
        inspect_page(FakePage("https://www.zhihu.com/signin?next=/", "旧回答还在：如何评价"), found_content=4)
        raise AssertionError("应该要求登录")
    except LoginExpiredError as exc:
        assert "登录" in str(exc)
    try:
        inspect_page(
            FakePage("https://www.zhihu.com/people/zhangsan/answers", "正常列表"),
            found_content=4,
            network_errors=[
                {
                    "url": "https://www.zhihu.com/api/v4/members/zhangsan/answers",
                    "status": 401,
                    "message": "请先登录",
                }
            ],
        )
        raise AssertionError("列表接口要求登录时应该停止")
    except LoginExpiredError:
        pass
    inspect_page(
        FakePage("https://www.zhihu.com/people/zhangsan/answers", "正常列表"),
        found_content=4,
        network_errors=[{"url": "https://www.zhihu.com/api/v4/me", "status": 401, "message": "请先登录"}],
    )


def test_counts_keep_answer_and_pin_with_the_same_id(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    answer = _record("5", "<p>回答</p>")
    pin = _record("5", "<p>想法</p>")
    pin.content_type = "pin"
    pin.url = "https://www.zhihu.com/pin/5"
    pin.question_id = ""
    _save(database, answer)
    _save(database, pin)
    author_url = "https://www.zhihu.com/people/zhangsan"
    assert database.count_contents(author_url=author_url, content_type="answer") == 1
    assert database.count_contents(author_url=author_url, content_type="pin") == 1
    assert database.count_contents(author_url=author_url) == 2


def test_failed_image_can_be_retried_without_new_content(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    _, local_id = _save(database, _record("5", "<p>图</p>"))
    database.replace_images(
        local_id,
        [{"original_url": "https://pic1.zhimg.com/a.jpg", "local_path": "", "download_status": "failed", "hash": "", "sort_index": 1}],
    )

    class Downloader:
        def download_all(self, urls, directory, content_id, **_kwargs):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "5_01.jpg"
            path.write_bytes(b"img")
            return [
                ImageAsset(
                    original_url=urls[0],
                    local_path=str(path),
                    download_status="downloaded",
                    hash="abc",
                    sort_index=1,
                )
            ]

    row = database.get_content(local_id)
    images = retry_failed_images(database, row, tmp_path / "images", Downloader())
    assert images[0]["download_status"] == "downloaded"
    assert images[0]["sort_index"] == 1
    stored = database.list_images(local_id)
    assert stored[0]["download_status"] == "downloaded"
