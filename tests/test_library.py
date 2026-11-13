import os

import httpx
import pytest

from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.offline import export_local_contents
from zhihu_collector.collectors.repairs import retry_saved_failures
from zhihu_collector.collectors.service import CollectRequest, write_separate_exports
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.downloader.image_downloader import ImageDownloader
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.parsers.html_cleaner import plain_preview


def _save(database: Database, content_id: str, html: str, *, title: str, author: str, kind: str = "answer"):
    record = ContentRecord(
        zhihu_content_id=content_id,
        content_type=kind,
        url=f"https://www.zhihu.com/question/1/answer/{content_id}" if kind == "answer" else f"https://www.zhihu.com/pin/{content_id}",
        title=title,
        author=AuthorInfo(zhihu_user_id=author, name=author, url_token=author, url=f"https://www.zhihu.com/people/{author}"),
        question_id="1" if kind == "answer" else "",
        question_title=title,
        html_content=html,
        updated_time=1700001000,
    )
    return database.save_content(record, markdown=html, html=html)[1]


def test_plain_preview_drops_script():
    text = plain_preview("<script>alert(1)</script><p>可见正文</p>")
    assert "可见正文" in text
    assert "alert" not in text
    assert "<script>" not in text


def test_search_filters_and_paginates(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    for index in range(5):
        _save(database, str(index), f"<p>甲的正文 {index}</p>", title=f"甲标题{index}", author="张三")
    _save(database, "pin-1", "<p>想法正文</p>", title="乙的想法", author="李四", kind="pin")
    rows, total = database.search_contents(keyword="甲的正文", limit=2)
    assert total == 5
    assert len(rows) == 2
    next_rows, _ = database.search_contents(keyword="甲的正文", offset=2, limit=2)
    assert {row["id"] for row in rows}.isdisjoint({row["id"] for row in next_rows})
    pins, pin_total = database.search_contents(content_type="pin")
    assert pin_total == 1
    assert pins[0]["title"] == "乙的想法"
    author_rows, author_total = database.search_contents(author="李四")
    assert author_total == 1
    assert author_rows[0]["content_type"] == "pin"


def test_offline_export_and_cancel_do_not_start_collection_browser(monkeypatch, tmp_path):
    def forbid_browser(self):
        raise AssertionError("离线导出不应启动采集浏览器")

    monkeypatch.setattr("zhihu_collector.browser.playwright_manager.BrowserSession.start", forbid_browser)
    database = Database(tmp_path / "zhihu.db")
    first = _save(database, "1", "<p>第一篇正文</p>", title="第一篇", author="zhang")
    second = _save(database, "2", "<p>第二篇正文</p>", title="第二篇", author="zhang")
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    request = CollectRequest(
        url="offline",
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
        export_separate=True,
        export_merged=True,
        save_metadata=False,
    )
    result = export_local_contents(database, [first, second], output, request, Settings())
    assert result.status == "success"
    texts = [path.read_text(encoding="utf-8") for path in output.rglob("*.md")]
    assert any("第一篇正文" in text for text in texts)
    assert any("第二篇正文" in text for text in texts)
    assert any("所选内容" in path.name for path in output.glob("*.md"))
    assert sentinel.read_text(encoding="utf-8") == "keep"

    controller = TaskController()

    def stop_after_first(done, total, title):
        if done >= 1:
            controller.stop()

    another = tmp_path / "cancel"
    another.mkdir()
    kept = another / "keep.txt"
    kept.write_text("keep", encoding="utf-8")
    cancelled = export_local_contents(
        database,
        [first, second],
        another,
        CollectRequest(url="offline", export_markdown=True, export_docx=False, export_pdf=False, export_merged=False),
        Settings(),
        controller,
        on_progress=stop_after_first,
    )
    assert cancelled.status == "cancelled"
    assert kept.read_text(encoding="utf-8") == "keep"
    assert len(list(another.rglob("*.md"))) == 1


def test_retry_failed_image_updates_markdown_without_recrawl(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    local_id = _save(
        database,
        "9",
        '<p>图文</p><img src="https://pic1.zhimg.com/a.jpg"/>',
        title="图文",
        author="zhang",
    )
    database.replace_images(
        local_id,
        [{"original_url": "https://pic1.zhimg.com/a.jpg", "local_path": "", "download_status": "failed", "hash": "", "sort_index": 2}],
    )
    directory = tmp_path / "回答"
    request = CollectRequest(
        url="offline",
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
        save_metadata=False,
    )
    write_separate_exports(database.get_content(local_id), [dict(image) for image in database.list_images(local_id)], directory, request, Settings(), None, database)
    markdown_path = next(directory.glob("*.md"))
    assert "https://pic1.zhimg.com/a.jpg" in markdown_path.read_text(encoding="utf-8")

    def handler(request):
        assert request.url.host == "pic1.zhimg.com"
        return httpx.Response(200, content=b"img-bytes", headers={"content-type": "image/jpeg"})

    downloader = ImageDownloader(concurrency=1, attempts=1, base_delay_ms=1, client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = retry_saved_failures(database, Settings(), tmp_path, save_metadata=False, downloader=downloader)
    downloader.close()
    assert result.status == "success"
    updated = markdown_path.read_text(encoding="utf-8")
    assert "https://pic1.zhimg.com/a.jpg" not in updated
    assert ".jpg" in updated
    stored = database.list_images(local_id)
    assert stored[0]["download_status"] == "downloaded"
    assert stored[0]["sort_index"] == 2


def test_retry_reports_login_without_damaging_the_file(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    local_id = _save(database, "3", "<p>保留这段</p>", title="保留", author="zhang")
    database.replace_images(
        local_id,
        [{"original_url": "https://pic1.zhimg.com/locked.jpg", "local_path": "", "download_status": "failed", "hash": "", "sort_index": 1}],
    )
    directory = tmp_path / "回答"
    write_separate_exports(
        database.get_content(local_id),
        [],
        directory,
        CollectRequest(url="offline", export_markdown=True, export_docx=False, export_pdf=False, save_metadata=False),
        Settings(),
        None,
        database,
    )
    markdown_path = next(directory.glob("*.md"))
    before = markdown_path.read_text(encoding="utf-8")

    def handler(request):
        return httpx.Response(403, text="请登录后继续")

    downloader = ImageDownloader(concurrency=1, attempts=1, base_delay_ms=1, client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = retry_saved_failures(database, Settings(), tmp_path, downloader=downloader)
    downloader.close()
    assert result.status == "needs_login"
    assert markdown_path.read_text(encoding="utf-8") == before


def test_library_and_failure_windows_open(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton

    from zhihu_collector.gui.failures_window import FailuresWindow
    from zhihu_collector.gui.library_window import LibraryWindow
    from zhihu_collector.gui.main_window import MainWindow

    settings = Settings(database_path=str(tmp_path / "zhihu.db"), output_dir=str(tmp_path / "output"))
    monkeypatch.setattr("zhihu_collector.gui.main_window.load_settings", lambda: settings)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    labels = [button.text() for button in window.findChildren(QPushButton)]
    assert "内容库" in labels
    assert "失败项" in labels
    local_id = _save(window.database, "8", "<script>alert(1)</script><p>可见正文</p>", title="库内标题", author="wang")
    library = LibraryWindow(window.database, window.settings)
    library.keyword.setText("库内")
    library.search_first()
    assert library.table.rowCount() == 1
    assert library.table.item(0, 1).text() == "库内标题"
    library.table.selectRow(0)
    app.processEvents()
    preview = library.preview.toPlainText()
    assert "可见正文" in preview
    assert "alert" not in preview
    window.database.replace_images(
        local_id,
        [{"original_url": "https://pic1.zhimg.com/x.jpg", "local_path": "", "download_status": "failed", "hash": "", "sort_index": 1}],
    )
    failures = FailuresWindow(window.database, window.settings)
    assert failures.table.rowCount() == 1
    assert failures.table.item(0, 0).text() == "图片"
    library.close()
    failures.close()
    window.close()
    app.processEvents()
