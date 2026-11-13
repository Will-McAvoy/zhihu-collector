"""集中管理知乎页面的 CSS 选择器。

这些选择器只用于网络 JSON 没有给全字段时的 DOM 兜底。
知乎改版后，优先更新本文件，而不是去改采集流程。
"""


class ZhihuSelectors:
    answer_card = ".List-item, .AnswerItem, .ContentItem"
    card_title = "h2 a, .ContentItem-title a, .QuestionItem-title"
    card_link = "a[href*='/answer/']"
    card_author = ".AuthorInfo-name, .UserLink-link, .AuthorInfo-head .UserLink-link"
    card_content = ".RichContent .RichText, .RichText, span[itemprop='text']"
    card_time = ".ContentItem-time, time"
    card_vote = ".VoteButton--up, button[aria-label*='赞同']"
    card_question_link = "a[href*='/question/']"
    read_more = "button.ContentItem-more, button:has-text('阅读全文')"
    load_more = "button:has-text('加载更多'), button:has-text('显示更多'), button:has-text('更多回答')"
    question_title = "h1.QuestionHeader-title, h1.QuestionHeader-title span, h1"
    question_detail = ".QuestionRichText, .QuestionHeader-detail .RichText"
    pin_card = ".List-item, .PinItem, .ContentItem"
    pin_content = ".RichText, .PinItem-content, .ContentItem-richText"
    empty_hint = ".List-empty, .ErrorPage, .PlaceHolder"
    user_name = ".ProfileHeader-name, .ProfileHeader-title"
    user_headline = ".ProfileHeader-headline"
