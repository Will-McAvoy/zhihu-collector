import httpx

from zhihu_collector.collectors.paging import ScrollState
from zhihu_collector.collectors.service import CollectRequest, write_separate_exports
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.downloader.image_downloader import ImageDownloader
from zhihu_collector.errors import LoginExpiredError, VerificationRequiredError
from zhihu_collector.exporters.docx_exporter import markdown_to_docx
from zhihu_collector.exporters.markdown_exporter import localize_images, render_markdown, render_merged_markdown
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.parsers.html_cleaner import clean_content
from zhihu_collector.browser.page_guard import inspect_page


HTML = """
<p>段落 <b>粗体</b> <i>斜体</i></p>
<blockquote>引用</blockquote>
<ul><li>甲</li></ul>
<pre><code class="language-python">print(1)</code></pre>
<img data-original="https://pic1.zhimg.com/a.jpg" src="data:image/gif;base64,aaaa">
<a href="https://link.zhihu.com/?target=https%3A%2F%2Fexample.com%2Fa">链接</a>
<script>alert(1)</script>
<button>点赞</button>
<span data-tex="E=mc^2"></span>
"""


class FakePage:
    def __init__(self, url: str, text: str):
        self.url = url
        self._text = text

    def inner_text(self, selector: str) -> str:
        return self._text


def test_clean_html_keeps_content_and_drops_ui():
    cleaned = clean_content(HTML)
    assert "段落" in cleaned.markdown
    assert "粗体" in cleaned.markdown
    assert "引用" in cleaned.markdown
    assert "example.com/a" in cleaned.markdown
    assert "https://pic1.zhimg.com/a.jpg" in cleaned.image_urls
    assert "alert" not in cleaned.markdown
    assert "点赞" not in cleaned.markdown
    assert "E=mc^2" in cleaned.markdown


def test_markdown_metadata_and_local_images(tmp_path):
    image = tmp_path / "images" / "1.jpg"
    image.parent.mkdir()
    image.write_bytes(b"img")
    body = "看图 ![](https://pic1.zhimg.com/a.jpg)"
    rendered = render_markdown(
        content_type="answer",
        title="如何评价：测试？",
        author="张三",
        question="如何评价：测试？",
        url="https://www.zhihu.com/question/1/answer/2",
        created_time="2025-01-01T00:00:00+08:00",
        updated_time="2025-02-01T00:00:00+08:00",
        vote_count=3,
        comment_count=1,
        body=body,
        include_meta=True,
    )
    destination = tmp_path / "回答" / "0001.md"
    localized = localize_images(
        rendered,
        [{"original_url": "https://pic1.zhimg.com/a.jpg", "local_path": str(image), "download_status": "downloaded"}],
        destination,
    )
    assert localized.startswith("---")
    assert "author: \"张三\"" in localized
    assert "../images/1.jpg" in localized
    merged = render_merged_markdown(
        title="张三全部回答",
        author="张三",
        pieces=[{"anchor": "item-1", "title": "如何评价", "author": "张三", "created_at": "2025-01-01", "url": "https://www.zhihu.com/a", "body": "正文"}],
        include_meta=True,
    )
    assert "## 目录" in merged
    assert "内容总数：1" in merged


def test_docx_uses_heading_style(tmp_path):
    path = tmp_path / "答案.docx"
    markdown_to_docx("# 如何评价\n\n这是正文 **重点**\n\n```python\nprint(1)\n```\n", path)
    from docx import Document

    document = Document(path)
    assert document.paragraphs[0].text == "如何评价"
    assert document.paragraphs[0].style.name.startswith("Heading")
    assert any("正文" in paragraph.text for paragraph in document.paragraphs)


def test_database_dedup_and_resume(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    record = ContentRecord(
        zhihu_content_id="1001",
        content_type="answer",
        url="https://www.zhihu.com/question/2/answer/1001",
        title="如何评价测试？",
        author=AuthorInfo(zhihu_user_id="u1", name="张三", url_token="zhangsan", url="https://www.zhihu.com/people/zhangsan"),
        question_id="2",
        question_title="如何评价测试？",
        question_url="https://www.zhihu.com/question/2",
        html_content="<p>正文</p>",
        created_time=1700000000,
        updated_time=1700001000,
        vote_count=4,
    )
    first, _ = database.save_content(record, markdown="正文", html="<p>正文</p>")
    second, _ = database.save_content(record, markdown="正文", html="<p>正文</p>")
    assert first == "inserted"
    assert second == "skipped"
    assert database.count_contents() == 1
    record.html_content = "<p>更新后的正文</p>"
    record.updated_time = 1700002000
    third, _ = database.save_content(record, markdown="更新后的正文", html="<p>更新后的正文</p>")
    assert third == "updated"
    assert database.count_contents() == 1
    task_id = database.create_task(
        task_type="user_answers",
        target_url="https://www.zhihu.com/people/zhangsan",
        target_id="zhangsan",
        output_dir=str(tmp_path),
        options={"url": "https://www.zhihu.com/people/zhangsan"},
    )
    database.update_task(task_id, status="running", total_completed=1, total_found=2)
    database.recover_interrupted_tasks()
    task = database.latest_task("https://www.zhihu.com/people/zhangsan")
    assert task["status"] == "interrupted"
    assert task["total_completed"] == 1
    versions = database.known_versions(author_url="https://www.zhihu.com/people/zhangsan", content_type="answer")
    assert "1001" in versions


def test_separate_export_and_resume_index(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    record = ContentRecord(
        zhihu_content_id="9",
        content_type="answer",
        url="https://www.zhihu.com/question/2/answer/9",
        title="为什么？",
        author=AuthorInfo(name="张三", url_token="zhangsan", url="https://www.zhihu.com/people/zhangsan", zhihu_user_id="u1"),
        question_id="2",
        question_title="为什么？",
        html_content="<p>因为如此</p>",
    )
    _, local_id = database.save_content(record, markdown="因为如此", html="<p>因为如此</p>")
    row = database.get_content(local_id)
    request = CollectRequest(url="https://www.zhihu.com/people/zhangsan", export_pdf=False)
    write_separate_exports(row, [], tmp_path / "回答", request, Settings(), None)
    files = list((tmp_path / "回答").glob("*.md"))
    assert len(files) == 1
    assert "因为如此" in files[0].read_text(encoding="utf-8")
    assert "?" not in files[0].name


def test_scroll_stops_after_repeated_empty_pages_and_supports_incremental_stop():
    state = ScrollState(max_stale_rounds=3)
    assert state.observe_page(["a"]) == ["a"]
    assert state.reached_end is False
    state.observe_page(["a"])
    state.observe_page(["a"])
    state.observe_page(["a"])
    assert state.reached_end is True
    ended = ScrollState()
    ended.observe_page(["a"], is_end=True)
    assert ended.stop_reason == "分页已经结束：接口声明已到末页"
    assert ended.end_evidence == "is_end"
    incremental = ScrollState()
    incremental.emitted_ids.update(["a", "b"])
    incremental.observe_page(
        ["a", "b"],
        update_only=True,
        sorted_by_time=True,
        unchanged_ids={"a", "b"},
    )
    assert incremental.reached_end is True


def test_login_wall_is_not_reported_as_captcha_solver_prompt():
    try:
        inspect_page(
            FakePage(
                "https://www.zhihu.com/account/unhuman?need_login=true",
                "请您登录后查看更多专业优质内容。 登录知乎",
            ),
            found_content=0,
        )
        raise AssertionError("应该要求登录")
    except LoginExpiredError as exc:
        assert "登录" in str(exc)
    try:
        inspect_page(FakePage("https://www.zhihu.com/account/unhuman?type=captcha", "请拖动滑块完成验证"), found_content=0)
        raise AssertionError("应该停在验证")
    except VerificationRequiredError as exc:
        assert "不会尝试自动处理验证码" in str(exc)


def test_image_download_retries_then_keeps_remote_url_on_failure(tmp_path):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if "ok" in str(request.url):
            if calls["count"] < 2:
                return httpx.Response(500)
            return httpx.Response(200, content=b"\xff\xd8\xff\xd9", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = ImageDownloader(concurrency=1, attempts=3, base_delay_ms=1, client=client)
    assets = downloader.download_all(
        ["https://pic1.zhimg.com/ok.jpg", "https://pic1.zhimg.com/missing.jpg"],
        tmp_path,
        "1001",
    )
    assert assets[0].download_status == "downloaded"
    assert assets[0].local_path.endswith(".jpg")
    assert assets[1].download_status == "failed"
    assert assets[1].original_url.endswith("missing.jpg")
    downloader.close()
