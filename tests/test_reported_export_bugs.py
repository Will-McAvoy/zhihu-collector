"""2026-10-13 实测发现的 Word 转义和离线跳过文件计数回归。"""
from dataclasses import replace
import pytest
from docx import Document

from zhihu_collector.collectors.offline import export_local_contents
from zhihu_collector.collectors.service import CollectRequest
from zhihu_collector.config import Settings
from zhihu_collector.database.database import Database
from zhihu_collector.exporters.docx_exporter import markdown_to_docx
from zhihu_collector.parsers.html_cleaner import html_to_markdown
from zhihu_collector.models import ContentRecord


@pytest.mark.parametrize("text", [
    "EXPORT_PROBE_20261013", "文件名 test_file.py", "星号 a*b*c",
    r"路径 C:\temp\test_file.py", r"字面反斜杠 \ 和下划线 _",
])
def test_html_to_word_preserves_literal_punctuation(tmp_path, text):
    dest = tmp_path / "literal.docx"
    markdown_to_docx(html_to_markdown(f"<p>{text}</p>"), dest)
    assert Document(dest).paragraphs[0].text == text


def test_word_escaped_symbols_and_nested_styles_keep_code_literal(tmp_path):
    dest = tmp_path / "styles.docx"
    markdown_to_docx(
        r"字面 \*星号\* **粗体 test\_file 与 *斜体*** `C:\temp\test_file.py`",
        dest,
    )
    paragraph = Document(dest).paragraphs[0]
    assert paragraph.text == r"字面 *星号* 粗体 test_file 与 斜体 C:\temp\test_file.py"
    bold = next(run for run in paragraph.runs if "test_file" in run.text)
    assert bold.bold and not bold.italic
    italic = next(run for run in paragraph.runs if run.text == "斜体")
    assert italic.bold and italic.italic
    code = next(run for run in paragraph.runs if run.text.startswith("C:"))
    assert code.font.name == "Consolas"
    assert not code.bold and not code.italic


def test_word_escapes_in_headings_lists_tables_and_links(tmp_path):
    dest = tmp_path / "blocks.docx"
    markdown_to_docx(
        "# 标题 test\\_file\n\n- 列表 test\\_file\n\n"
        "| 名称 |\n| --- |\n| test\\_file |\n\n"
        "[test\\_file](https://example.com/test_file)\n",
        dest,
    )
    doc = Document(dest)
    assert doc.paragraphs[0].text == "标题 test_file"
    assert doc.paragraphs[1].text == "列表 test_file"
    assert doc.tables[0].cell(1, 0).text == "test_file"
    link = doc.paragraphs[-1].hyperlinks[0]
    assert link.text == "test_file"
    assert link.url == "https://example.com/test_file"


@pytest.mark.parametrize("separate,merged,expected", [(True, False, 2), (False, True, 2), (True, True, 4)])
def test_offline_skip_counts_files_across_formats(tmp_path, separate, merged, expected):
    db = Database(tmp_path / "test.db")
    try:
        record = ContentRecord("900001", "answer", "https://www.zhihu.com/answer/900001", title="测试", html_content="<p>正文</p>")
        cid = db.save_content(record, markdown="正文", html=record.html_content)[1]
        output = tmp_path / "output"
        req = CollectRequest(url=record.url, export_pdf=False, download_images=False, export_separate=separate, export_merged=merged)
        assert export_local_contents(db, [cid], output, req, Settings()).status == "success"
        before = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}
        assert len(before) == expected
        result = export_local_contents(db, [cid], output, replace(req, conflict_policy="skip"), Settings())
        assert result.status == "skipped"
        assert result.completed == result.failed == 0
        assert result.skipped == expected
        assert f"跳过 {expected} 个已有文件" in result.message
        assert {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()} == before
    finally:
        db.close()


def test_offline_partial_skip_counts_existing_format_when_other_written(tmp_path):
    db = Database(tmp_path / "test.db")
    try:
        record = ContentRecord("900002", "answer", "https://www.zhihu.com/answer/900002", title="测试", html_content="<p>正文</p>")
        cid = db.save_content(record, markdown="正文", html=record.html_content)[1]
        output = tmp_path / "output"
        req = CollectRequest(url=record.url, export_pdf=False, export_docx=False, download_images=False)
        export_local_contents(db, [cid], output, req, Settings())
        result = export_local_contents(db, [cid], output, replace(req, export_docx=True, conflict_policy="skip"), Settings())
        assert result.status == "success"
        assert result.completed == 1 and result.failed == 0
        assert result.skipped == 1
        assert "跳过 1 个已有文件" in result.message
        assert len(list(output.rglob("*.docx"))) == 1
    finally:
        db.close()


def test_old_word_export_is_regenerated_as_copy_and_then_reused(tmp_path, monkeypatch):
    import zhihu_collector.collectors.service as service
    db = Database(tmp_path / "test.db")
    try:
        record = ContentRecord("900003", "answer", "https://www.zhihu.com/answer/900003", title="测试", html_content="<p>test_file</p>")
        cid = db.save_content(record, markdown="test_file", html=record.html_content)[1]
        row = db.get_content(cid)
        req = CollectRequest(url=record.url, export_markdown=False, export_pdf=False, download_images=False)
        directory = tmp_path / "output"
        original_fingerprint = service._export_fingerprint
        with monkeypatch.context() as patch:
            patch.setattr(service, "_export_fingerprint", lambda *args, **kwargs: original_fingerprint(*args))
            service.write_separate_exports(row, [], directory, req, Settings(), database=db)
        old = next(directory.glob("*.docx"))
        old_bytes = old.read_bytes()
        service.write_separate_exports(row, [], directory, req, Settings(), database=db)
        copies = list(directory.glob("*_副本1.docx"))
        assert len(copies) == 1
        assert old.read_bytes() == old_bytes
        assert any(p.text == "test_file" for p in Document(copies[0]).paragraphs)
        service.write_separate_exports(row, [], directory, req, Settings(), database=db)
        assert len(list(directory.glob("*.docx"))) == 2
    finally:
        db.close()
