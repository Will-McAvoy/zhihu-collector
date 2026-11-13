import json
from pathlib import Path

from zhihu_collector.collectors.user_answers import records_from_page_sources
from zhihu_collector.models import dedupe_records
from zhihu_collector.parsers.answer_parser import parse_answer_payloads
from zhihu_collector.parsers.dom_parser import parse_answer_cards_html, parse_primary_content_html
from zhihu_collector.poc import build_poc_document

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_member_answers_and_dedupe():
    payload = json.loads((FIXTURES / "member_answers.json").read_text(encoding="utf-8"))
    records = dedupe_records(parse_answer_payloads([payload]))
    assert len(records) == 2
    first = records[0]
    assert first.zhihu_content_id == "1001"
    assert first.question_title == "如何评价测试？"
    assert first.author.name == "张三"
    assert first.url == "https://www.zhihu.com/question/2002/answer/1001"
    assert first.created_time == 1700000000
    assert "你好" in first.html_content
    assert first.image_urls == ["https://pic1.zhimg.com/a.jpg"]
    assert first.vote_count == 10
    assert records[1].author.name == "匿名用户"
    assert records[1].url == "https://www.zhihu.com/question/2003/answer/1002"


def test_dom_card_and_merge_prefers_longer_html():
    html = (FIXTURES / "answer_card.html").read_text(encoding="utf-8")
    payload = {
        "data": [
            {
                "id": 1001,
                "type": "answer",
                "excerpt": "只有摘要",
                "question": {"id": 2002, "title": "如何评价测试？"},
                "author": {"name": "张三", "url_token": "zhangsan"},
            }
        ]
    }
    records = records_from_page_sources(payloads=[payload], html=html, user_token="zhangsan")
    assert len(records) == 1
    assert "DOM 正文" in records[0].html_content
    assert records[0].author.name == "张三"


def test_filters_other_authors():
    payload = {
        "data": [
            {
                "id": 1,
                "type": "answer",
                "content": "<p>自己的</p>",
                "question": {"id": 2, "title": "问题"},
                "author": {"name": "张三", "url_token": "zhangsan"},
            },
            {
                "id": 3,
                "type": "answer",
                "content": "<p>别人的推荐</p>",
                "question": {"id": 4, "title": "另一题"},
                "author": {"name": "李四", "url_token": "lisi"},
            },
        ]
    }
    records = records_from_page_sources(payloads=[payload], html="", user_token="zhangsan")
    assert [item.zhihu_content_id for item in records] == ["1"]


def test_primary_rich_text_and_poc_document():
    html = "<div class='QuestionAnswer-content'><div class='RichText'><p>详情页正文足够长的一段话</p></div></div>"
    assert "详情页正文" in parse_primary_content_html(html)
    cards = parse_answer_cards_html((FIXTURES / "answer_card.html").read_text(encoding="utf-8"))
    document = build_poc_document("https://www.zhihu.com/people/zhangsan", cards, headless=True, login_state_loaded=False)
    assert document["count"] == 1
    assert document["headless"] is True
    assert document["answers"][0]["question_title"] == "如何评价测试？"
