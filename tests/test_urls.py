from zhihu_collector.utils.urls import parse_zhihu_url


def test_people_url_with_answers_tab():
    parsed = parse_zhihu_url("https://www.zhihu.com/people/excited-vczh/answers")
    assert parsed.kind == "user"
    assert parsed.user_token == "excited-vczh"
    assert parsed.user_kind == "people"
    assert parsed.canonical == "https://www.zhihu.com/people/excited-vczh"
    assert parsed.answers_page_url == "https://www.zhihu.com/people/excited-vczh/answers"


def test_org_and_mobile_host():
    parsed = parse_zhihu_url("https://m.zhihu.com/org/some-org?utm=1")
    assert parsed.kind == "user"
    assert parsed.user_kind == "org"
    assert parsed.user_token == "some-org"
    assert parsed.pins_page_url == "https://www.zhihu.com/org/some-org/pins"


def test_question_url():
    parsed = parse_zhihu_url("https://www.zhihu.com/question/123456789?sort=created")
    assert parsed.kind == "question"
    assert parsed.question_id == "123456789"
    assert parsed.canonical == "https://www.zhihu.com/question/123456789"


def test_answer_url():
    parsed = parse_zhihu_url("https://zhihu.com/question/123/answer/456")
    assert parsed.kind == "answer"
    assert parsed.question_id == "123"
    assert parsed.answer_id == "456"
    assert parsed.canonical == "https://www.zhihu.com/question/123/answer/456"


def test_unknown_url():
    parsed = parse_zhihu_url("https://example.com/people/abc")
    assert parsed.kind == "unknown"
