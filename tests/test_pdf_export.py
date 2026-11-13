from zhihu_collector.browser.playwright_manager import BrowserSession
from zhihu_collector.exporters.pdf_exporter import PdfExporter


def test_pdf_export_writes_a_file(tmp_path):
    exporter = PdfExporter()
    destination = tmp_path / "合并.pdf"
    with BrowserSession(headless=True) as session:
        exporter.session = session
        exporter.write("# 张三的回答\n\n## 目录\n\n# 第一节\n\n正文\n", destination, title="张三的回答")
    assert destination.is_file()
    assert destination.stat().st_size > 1000
