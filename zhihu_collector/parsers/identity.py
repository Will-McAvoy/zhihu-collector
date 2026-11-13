"""DOM fallback must prove the complete answer identity before using its body."""
import json
from bs4 import BeautifulSoup
from zhihu_collector.utils.urls import parse_zhihu_url


def answer_container(soup: BeautifulSoup, answer_id: str):
    wanted = str(answer_id)
    if not wanted.isdecimal():
        return None
    for node in soup.select('.AnswerItem, .ContentItem, .List-item, [data-zop]'):
        identities = set()
        name = node.get('name')
        if name and str(name).isdecimal():
            identities.add(str(name))
        try:
            data = json.loads(node.get('data-zop', '{}'))
        except (ValueError, TypeError):
            data = {}
        if isinstance(data, dict) and data.get('type', 'answer') == 'answer':
            value = data.get('itemId')
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isdecimal():
                identities.add(str(value))
        # A card containing multiple answers cannot identify a single body.
        for link in node.select('a[href]'):
            if link.find_parent(class_='RichText') is not None:
                continue  # Citations inside the body do not identify this answer.
            href = link.get('href', '')
            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                href = 'https://www.zhihu.com' + href
            parsed = parse_zhihu_url(href)
            if parsed.kind == 'answer' and parsed.answer_id:
                identities.add(parsed.answer_id)
        if identities == {wanted}:
            return node
    return None
