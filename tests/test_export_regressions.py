"""三个已复现问题的回归测试。修复前应当失败。"""

import httpx

from zhihu_collector.collectors.service import CollectRequest, write_separate_exports
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.downloader.image_downloader import ImageDownloader
from zhihu_collector.models import AuthorInfo, ContentRecord


def _author() -> AuthorInfo:
    return AuthorInfo(
        zhihu_user_id="u1",
        name="张三",
        url_token="zhangsan",
        url="https://www.zhihu.com/people/zhangsan",
    )


def _record(content_id: str, html: str, title: str = "同样的标题") -> ContentRecord:
    return ContentRecord(
        zhihu_content_id=content_id,
        content_type="answer",
        url=f"https://www.zhihu.com/question/2/answer/{content_id}",
        title=title,
        author=_author(),
        question_id="2",
        question_title=title,
        question_url="https://www.zhihu.com/question/2",
        html_content=html,
        updated_time=1700001000,
    )


def _save(database: Database, record: ContentRecord):
    return database.save_content(record, markdown=record.html_content, html=record.html_content)


class MarkerPdf:
    """小型合法 PDF，测试文件命名时不启动浏览器。标记保存为 PDF 注释。"""

    def write(self, markdown_text, destination, *, title):
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = b"%PDF-1.4\n"
        data += "".join(f"% {line}\n" for line in markdown_text.splitlines()).encode("utf-8")
        offsets = [0]
        for idx, obj in enumerate((
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] >>",
        ), start=1):
            offsets.append(len(data))
            data += f"{idx} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
        xref = len(data)
        data += b"xref\n0 4\n0000000000 65535 f \n"
        data += "".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]).encode("ascii")
        data += f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
        destination.write_bytes(data)
        return destination


def test_pdf_only_same_title_does_not_overwrite(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    settings = Settings(filename_rule="{title}")
    request = CollectRequest(
        url="https://www.zhihu.com/people/zhangsan",
        export_markdown=False,
        export_docx=False,
        export_pdf=True,
        save_metadata=False,
    )
    _, first_id = _save(database, _record("11", "<p>甲的正文</p>"))
    _, second_id = _save(database, _record("22", "<p>乙的正文</p>"))
    directory = tmp_path / "回答"
    pdf = MarkerPdf()
    write_separate_exports(database.get_content(first_id), [], directory, request, settings, pdf, database)
    write_separate_exports(database.get_content(second_id), [], directory, request, settings, pdf, database)
    files = sorted(directory.glob("*.pdf"))
    assert len(files) == 2
    texts = {path.read_text(encoding="utf-8") for path in files}
    assert any("甲的正文" in text for text in texts)
    assert any("乙的正文" in text for text in texts)


def test_word_only_same_title_and_existing_file_and_repeat(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    settings = Settings(filename_rule="{title}")
    request = CollectRequest(
        url="https://www.zhihu.com/people/zhangsan",
        export_markdown=False,
        export_docx=True,
        export_pdf=False,
        save_metadata=False,
    )
    directory = tmp_path / "回答"
    directory.mkdir()
    preexisting = directory / "同样的标题.docx"
    preexisting.write_bytes(b"foreign-file")
    _, first_id = _save(database, _record("11", "<p>甲的正文</p>"))
    _, second_id = _save(database, _record("22", "<p>乙的正文</p>"))
    write_separate_exports(database.get_content(first_id), [], directory, request, settings, None, database)
    write_separate_exports(database.get_content(second_id), [], directory, request, settings, None, database)
    assert preexisting.read_bytes() == b"foreign-file"
    documents = [path for path in directory.glob("*.docx") if path != preexisting]
    assert len(documents) == 2
    from docx import Document

    texts = {"\n".join(paragraph.text for paragraph in Document(path).paragraphs) for path in documents}
    assert any("甲的正文" in text for text in texts)
    assert any("乙的正文" in text for text in texts)
    write_separate_exports(database.get_content(first_id), [], directory, request, settings, None, database)
    assert len([path for path in directory.glob("*.docx") if path != preexisting]) == 2
    both = CollectRequest(
        url="https://www.zhihu.com/people/zhangsan",
        export_markdown=False,
        export_docx=True,
        export_pdf=True,
        save_metadata=False,
    )
    write_separate_exports(database.get_content(first_id), [], directory, both, settings, MarkerPdf(), database)
    pdfs = list(directory.glob("*.pdf"))
    assert len(pdfs) == 1
    assert pdfs[0].stem == next(path.stem for path in documents if "甲的正文" in "\n".join(p.text for p in Document(path).paragraphs))


def test_retry_does_not_reuse_another_images_file(tmp_path):
    directory = tmp_path / "images"
    directory.mkdir()
    existing = directory / "1_01.jpg"
    existing.write_bytes(b"IMAGE-ONE")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"IMAGE-TWO", headers={"content-type": "image/jpeg"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = ImageDownloader(concurrency=1, attempts=1, base_delay_ms=1, client=client)
    assets = downloader.download_all(["https://pic1.zhimg.com/second.jpg"], directory, "1")
    downloader.close()
    assert calls == ["https://pic1.zhimg.com/second.jpg"]
    assert assets[0].download_status == "downloaded"
    assert assets[0].local_path
    saved = __import__("pathlib").Path(assets[0].local_path)
    assert saved.read_bytes() == b"IMAGE-TWO"
    assert saved.resolve() != existing.resolve()
    assert existing.read_bytes() == b"IMAGE-ONE"


def test_same_url_for_answer_and_pin_do_not_share_a_file(tmp_path):
    calls: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = b"ANSWER" if not calls else b"PIN"
        calls.append(body)
        return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = ImageDownloader(concurrency=1, attempts=1, base_delay_ms=1, client=client)
    directory = tmp_path / "images"
    url = "https://pic1.zhimg.com/shared.jpg"
    answer = downloader.download_all([url], directory, "answer_5")
    pin = downloader.download_all([url], directory, "pin_5")
    downloader.close()
    assert len(calls) == 2
    assert answer[0].local_path != pin[0].local_path
    assert __import__("pathlib").Path(answer[0].local_path).read_bytes() == b"ANSWER"
    assert __import__("pathlib").Path(pin[0].local_path).read_bytes() == b"PIN"


def test_metadata_toggle_and_image_recovery_update_the_file(tmp_path):
    database = Database(tmp_path / "zhihu.db")
    html = '<p>正文句子</p><img src="https://pic1.zhimg.com/a.jpg"/>'
    _, local_id = _save(database, _record("9", html, title="带图"))
    directory = tmp_path / "回答"
    settings = Settings(filename_rule="{index:04d}-{title}")
    images = [
        {
            "original_url": "https://pic1.zhimg.com/a.jpg",
            "local_path": "",
            "download_status": "failed",
            "hash": "",
            "sort_index": 1,
        }
    ]
    with_meta = CollectRequest(
        url="https://www.zhihu.com/people/zhangsan",
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
        save_metadata=True,
        conflict_policy="overwrite",
    )
    write_separate_exports(database.get_content(local_id), images, directory, with_meta, settings, None, database)
    markdown_path = next(directory.glob("*.md"))
    assert "作者：" in markdown_path.read_text(encoding="utf-8")
    without_meta = CollectRequest(
        url="https://www.zhihu.com/people/zhangsan",
        export_markdown=True,
        export_docx=False,
        export_pdf=False,
        save_metadata=False,
        conflict_policy="overwrite",
    )
    write_separate_exports(database.get_content(local_id), images, directory, without_meta, settings, None, database)
    text = markdown_path.read_text(encoding="utf-8")
    assert "作者：" not in text
    assert "正文句子" in text
    assert "https://pic1.zhimg.com/a.jpg" in text
    local_image = tmp_path / "images" / "a.jpg"
    local_image.parent.mkdir()
    local_image.write_bytes(b"img")
    recovered = [
        {
            "original_url": "https://pic1.zhimg.com/a.jpg",
            "local_path": str(local_image),
            "download_status": "downloaded",
            "hash": "abc",
            "sort_index": 1,
        }
    ]
    write_separate_exports(database.get_content(local_id), recovered, directory, without_meta, settings, None, database)
    updated = markdown_path.read_text(encoding="utf-8")
    assert "https://pic1.zhimg.com/a.jpg" not in updated
    assert "a.jpg" in updated
