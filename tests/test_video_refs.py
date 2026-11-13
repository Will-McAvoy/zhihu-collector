"""回答正文里的视频只保留标题和观看入口。"""

import zipfile

from zhihu_collector.collectors.service import _body_is_usable
from zhihu_collector.exporters.docx_exporter import markdown_to_docx
from zhihu_collector.exporters.pdf_exporter import markdown_to_html
from zhihu_collector.models import ContentRecord
from zhihu_collector.parsers.html_cleaner import clean_content, html_to_markdown

ANSWER = "https://www.zhihu.com/question/1/answer/2"
VIDEO = "https://www.zhihu.com/zvideo/123456"


def test_video_card_stays_between_paragraphs_with_title_and_link():
    raw = f"""
    <p>前文</p>
    <div class="video-box" data-name="原视频标题">
      <a href="{VIDEO}"><img src="https://pic.zhimg.com/cover.jpg" alt="原视频标题"></a>
    </div>
    <p>后文</p>
    """
    cleaned = clean_content(raw, source_url=ANSWER)
    assert cleaned.video_count == 1
    assert cleaned.video_fallback_count == 0
    assert cleaned.html.index("前文") < cleaned.html.index("[视频]") < cleaned.html.index("后文")
    assert "原视频标题" in cleaned.markdown
    assert VIDEO in cleaned.markdown
    assert "视频未下载，需联网观看" in cleaned.markdown
    assert "https://pic.zhimg.com/cover.jpg" in cleaned.image_urls


def test_video_and_source_are_kept_before_attributes_are_cleared():
    raw = """
    <p>甲</p>
    <video poster="https://pic.zhimg.com/poster.jpg">
      <source src="https://vdn.example/a.mp4?auth_key=secret">
    </video>
    <p>乙</p>
    """
    cleaned = clean_content(raw, source_url=ANSWER)
    assert cleaned.video_count == 1
    assert cleaned.video_fallback_count == 1
    assert "auth_key" not in cleaned.html
    assert ".mp4" not in cleaned.html
    assert "未取得独立视频链接" in cleaned.markdown
    assert ANSWER in cleaned.markdown
    assert cleaned.html.index("甲") < cleaned.html.index("[视频]") < cleaned.html.index("乙")
    assert "https://pic.zhimg.com/poster.jpg" in cleaned.image_urls


def test_video_iframe_becomes_a_link_and_other_iframes_do_not():
    raw = """
    <iframe src="https://www.zhihu.com/zvideo/9"></iframe>
    <p>地图</p>
    <iframe src="https://example.com/map"></iframe>
    <p>见 <a href="https://www.bilibili.com/video/BV1">普通链接</a></p>
    <img src="https://pic.zhimg.com/plain.jpg" alt="配图">
    """
    cleaned = clean_content(raw, source_url=ANSWER)
    assert cleaned.video_count == 1
    assert "https://www.zhihu.com/zvideo/9" in cleaned.markdown
    assert "<iframe" not in cleaned.html
    assert "普通链接" in cleaned.markdown
    assert "https://www.bilibili.com/video/BV1" in cleaned.markdown
    assert cleaned.markdown.count("[视频]") == 1
    assert "https://pic.zhimg.com/plain.jpg" in cleaned.image_urls


def test_missing_title_and_media_only_url_use_explicit_fallback():
    raw = '<video src="https://vdn.example/clip.mp4?token=abc"></video>'
    cleaned = clean_content(raw, source_url=ANSWER)
    assert "视频（原文未提供标题）" in cleaned.markdown
    assert "token=" not in cleaned.html
    assert ".mp4" not in cleaned.html
    assert ANSWER in cleaned.markdown
    assert cleaned.video_fallback_count == 1


def test_nested_player_makes_one_placeholder_and_repeat_keeps_two():
    raw = f"""
    <div class="video-box" data-name="只计一次">
      <video src="https://vdn.example/a.mp4"><source src="https://vdn.example/a.mp4"></video>
      <a href="{VIDEO}"><img src="https://pic.zhimg.com/cover.jpg" alt="只计一次"></a>
    </div>
    <div class="video-box" data-name="第二处">
      <a href="{VIDEO}"><img src="https://pic.zhimg.com/cover.jpg"></a>
    </div>
    """
    cleaned = clean_content(raw, source_url=ANSWER)
    assert cleaned.video_count == 2
    assert cleaned.markdown.count("[视频]") == 2
    assert "只计一次" in cleaned.markdown
    assert "第二处" in cleaned.markdown


def test_malicious_title_and_url_do_not_become_a_script_or_break_markup(tmp_path):
    raw = """
    <div class="video-box" data-name="a](javascript:alert(1)) &amp; &lt;b&gt;" data-lens-id="1">
      <a href="javascript:alert(1)"><img src="https://pic.zhimg.com/cover.jpg"></a>
    </div>
    <video src="javascript:alert(1)"></video>
    """
    cleaned = clean_content(raw, source_url=ANSWER)
    assert 'href="javascript:' not in cleaned.html
    assert "<script" not in cleaned.html
    assert "<b>" not in cleaned.html
    assert "］(javascript:" in cleaned.html or "javascript:" not in cleaned.html
    assert ANSWER in cleaned.markdown
    html = markdown_to_html(cleaned.markdown, title="视频", base_dir=tmp_path)
    assert 'href="javascript:' not in html
    assert ANSWER in html
    destination = tmp_path / "video.docx"
    markdown_to_docx(cleaned.markdown, destination)
    with zipfile.ZipFile(destination) as package:
        rels = package.read("word/_rels/document.xml.rels").decode("utf-8")
        document = package.read("word/document.xml").decode("utf-8")
    assert "javascript:" not in rels
    assert ANSWER in rels or ANSWER in document


def test_video_only_answer_is_usable_and_second_clean_stays_offline():
    raw = f'<video src="https://vdn.example/a.mp4"></video>'
    record = ContentRecord(zhihu_content_id="2", content_type="answer", url=ANSWER, html_content=raw)
    assert _body_is_usable(record) is True
    cleaned = clean_content(raw, source_url=ANSWER)
    again = html_to_markdown(cleaned.html)
    assert ANSWER in again
    assert "视频未下载" in again
    assert ".mp4" not in again
