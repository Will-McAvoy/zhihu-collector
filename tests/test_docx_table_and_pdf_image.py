from pathlib import Path
from docx import Document
from zhihu_collector.exporters.docx_exporter import markdown_to_docx
from zhihu_collector.exporters.pdf_exporter import markdown_to_html, PdfExporter
from zhihu_collector.browser.page_guard import inspect_page
from zhihu_collector.errors import VerificationRequiredError, LoginExpiredError

class FakePage:
    def __init__(self, url: str, text: str = ""):
        self.url = url
        self._text = text

    def inner_text(self, selector: str) -> str:
        return self._text


def test_docx_markdown_table_conversion(tmp_path):
    md = """# 包含表格
| 姓名 | 年龄 |
| --- | --- |
| 张三 | 18 |
| 李四 | 20 |
"""
    dest = tmp_path / "table.docx"
    markdown_to_docx(md, dest)
    doc = Document(dest)
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert len(table.rows) == 3
    assert len(table.columns) == 2
    assert table.cell(0, 0).text.strip() == "姓名"
    assert table.cell(1, 0).text.strip() == "张三"
    assert table.cell(2, 1).text.strip() == "20"


def test_pdf_image_base64_inlining(tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    md = f"# 图片测试\n![测试图片](test.png)"
    html_out = markdown_to_html(md, title="测试", base_dir=tmp_path)
    assert "data:image/png;base64," in html_out
    assert "border-collapse: collapse;" in html_out


def test_page_guard_catches_zhihu_restrictions():
    # 1. Network 403 parameter error
    try:
        inspect_page(
            FakePage("https://www.zhihu.com/people/someone/answers"),
            found_content=0,
            network_errors=[{"url": "https://www.zhihu.com/api/v4/members/someone/answers", "status": 403, "message": "请求参数异常，请升级客户端后重试。"}]
        )
        raise AssertionError("应当捕获 403 限制")
    except VerificationRequiredError as exc:
        assert "验证" in str(exc) or "受限" in str(exc) or "403" in str(exc)

    # 2. Page shows system busy
    try:
        inspect_page(
            FakePage("https://www.zhihu.com/people/someone/answers", "我的回答 系统繁忙，请稍后再试... 重新加载"),
            found_content=0,
        )
        raise AssertionError("页面系统繁忙应当捕获")
    except VerificationRequiredError:
        pass

    # 3. 40362 access restricted
    try:
        inspect_page(
            FakePage("https://www.zhihu.com/people/someone/answers", "您当前请求存在异常，暂时限制本次访问 错误码: 40362"),
            found_content=0,
        )
        raise AssertionError("40362 应当捕获")
    except VerificationRequiredError:
        pass
