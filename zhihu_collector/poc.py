"""最小采集验证：答主主页 -> 无头 Chromium -> 第一页公开回答 -> JSON。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from zhihu_collector.browser.auth_manager import AuthManager
from zhihu_collector.browser.playwright_manager import BrowserSession
from zhihu_collector.collectors.user_answers import UserAnswerCollector
from zhihu_collector.config import data_dir, ensure_runtime_dirs
from zhihu_collector.errors import CollectorError, UnsupportedUrlError
from zhihu_collector.utils.logger import get_logger
from zhihu_collector.utils.time_utils import now_iso
from zhihu_collector.utils.urls import parse_zhihu_url

logger = get_logger("poc")


def build_poc_document(url: str, records: list, *, headless: bool, login_state_loaded: bool) -> dict:
    return {
        "source_url": url,
        "collected_at": now_iso(),
        "headless": headless,
        "login_state_loaded": login_state_loaded,
        "count": len(records),
        "answers": [record.as_dict() for record in records],
    }


def run_poc(url: str, output: Path, *, headless: bool = True, fetch_missing_content: bool = True) -> dict:
    parsed = parse_zhihu_url(url)
    if parsed.kind != "user" or not parsed.answers_page_url:
        raise UnsupportedUrlError(url)
    ensure_runtime_dirs()
    auth = AuthManager()
    output.parent.mkdir(parents=True, exist_ok=True)
    with BrowserSession(headless=headless, storage_state=auth.state_path if auth.has_saved_state() else None) as session:
        collector = UserAnswerCollector(session)
        try:
            records = collector.collect_first_page(
                parsed.answers_page_url,
                user_token=parsed.user_token or "",
                fetch_missing_content=fetch_missing_content,
            )
        except CollectorError:
            _dump_open_pages(session, output)
            raise
        summary_path = output.with_name(output.stem + "_network.json")
        summary_path.write_text(
            json.dumps(collector.listener.summary(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not records:
            debug_html = output.with_name(output.stem + "_page.html")
            try:
                page = session.context.pages[-1] if session.context and session.context.pages else None
                if page is not None:
                    debug_html.write_text(page.content(), encoding="utf-8")
                    page.screenshot(path=str(output.with_name(output.stem + "_page.png")), full_page=False)
            except Exception as exc:
                logger.warning("保存调试页面失败：%s", exc)
    document = build_poc_document(
        parsed.canonical,
        records,
        headless=headless,
        login_state_loaded=auth.has_saved_state(),
    )
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return document


def _dump_open_pages(session: BrowserSession, output: Path) -> None:
    if session.context is None:
        return
    for index, page in enumerate(session.context.pages):
        try:
            logger.error("调试页面 %s 停留在 %s", index, page.url)
            output.with_name(f"{output.stem}_page{index}.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(output.with_name(f"{output.stem}_page{index}.png")))
        except Exception as exc:
            logger.warning("保存调试页面失败：%s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集知乎答主第一页公开回答并保存为 JSON")
    parser.add_argument("url", help="知乎答主主页，例如 https://www.zhihu.com/people/xxxx")
    parser.add_argument("--output", default="", help="JSON 输出路径")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口，仅用于调试")
    parser.add_argument("--no-details", action="store_true", help="列表没有正文时也不再逐条打开回答")
    args = parser.parse_args(argv)
    output = Path(args.output) if args.output else data_dir() / "output" / "poc_first_page.json"
    try:
        document = run_poc(
            args.url,
            output,
            headless=not args.headful,
            fetch_missing_content=not args.no_details,
        )
    except CollectorError as exc:
        logger.error("%s", exc)
        print(exc)
        return 2
    except Exception as exc:
        logger.exception("PoC 失败")
        print(f"采集失败：{exc}")
        return 1
    print(f"采集完成：{document['count']} 条")
    print(f"无头模式：{'是' if document['headless'] else '否'}")
    print(f"已加载登录状态：{'是' if document['login_state_loaded'] else '否'}")
    print(f"输出文件：{output}")
    for item in document["answers"][:3]:
        author = (item.get("author") or {}).get("name") or ""
        content = item.get("html_content") or ""
        print(f"- {item.get('zhihu_content_id')} | {author} | {item.get('question_title')} | 正文{len(content)}字")
    return 0 if document["count"] else 3


if __name__ == "__main__":
    sys.exit(main())
