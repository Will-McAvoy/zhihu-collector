"""一次采集任务：滚动列表、入库、下载图片、导出。"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from zhihu_collector.browser.auth_manager import AuthManager
from zhihu_collector.parsers.identity import answer_container
from zhihu_collector.browser.network_listener import NetworkListener
from zhihu_collector.browser.page_guard import inspect_page
from zhihu_collector.browser.navigation import explain_failure, failure_diagnostic, navigation_once
from zhihu_collector.browser.guarded import BrowserOperationTimeout
from zhihu_collector.browser.page_content import PageContentNotReady, read_page_content
from zhihu_collector.browser.playwright_manager import BrowserSession
from zhihu_collector.collectors.base import Progress, TaskController
from zhihu_collector.collectors.diagnostics import append_jsonl
from zhihu_collector.collectors.listing import collect_listing
from zhihu_collector.collectors.question_answers import parse_question_page
from zhihu_collector.collectors.user_answers import records_from_page_sources
from zhihu_collector.collectors.user_pins import parse_pin_page
from bs4 import BeautifulSoup

from zhihu_collector.config import Settings, log_dir
from zhihu_collector.database.database import Database, serialized_export
from zhihu_collector.downloader.image_downloader import ImageDownloader
from zhihu_collector.errors import CollectorError, CollectionScopeReady, LoginExpiredError, TaskStopped, UnsupportedUrlError, VerificationRequiredError
from zhihu_collector.exporters.docx_exporter import add_toc_field, markdown_to_docx
from zhihu_collector.exporters.markdown_exporter import localize_images, render_markdown, render_merged_markdown
from zhihu_collector.exporters.pdf_exporter import PdfExporter
from zhihu_collector.export_plan import ExportPlan, expected_export_file_count
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.parsers.dom_parser import parse_answer_cards_html, parse_primary_content_html
from zhihu_collector.parsers.embedded_json import extract_embedded_payloads
from zhihu_collector.parsers.pin_parser import parse_pin_payloads
from zhihu_collector.parsers.html_cleaner import clean_content, html_to_markdown
from zhihu_collector.parsers.video_refs import html_has_video_reference
from zhihu_collector.utils.artifacts import artifact_problem, file_digest, image_is_ready, image_problem
from zhihu_collector.utils.file_utils import apply_filename_rule, safe_filename
from zhihu_collector.utils.hashing import sha256_text
from zhihu_collector.utils.list_target import ListTarget, closure_notice, success_summary
from zhihu_collector.utils.logger import get_logger
from zhihu_collector.utils.urls import parse_zhihu_url, public_user_url

logger = get_logger("service")


class LimitReached(Exception):
    """已达到设定的采集数量上限。"""


@dataclass
class CollectRequest:
    url: str
    mode: str = "auto"
    output_dir: str = ""
    download_images: bool = True
    save_metadata: bool = True
    export_markdown: bool = True
    export_docx: bool = True
    export_pdf: bool = True
    export_separate: bool = True
    export_merged: bool = False
    update_only: bool = False
    max_items: int = 0
    conflict_policy: str = "new_copy"
    retry_content_ids: list[str] | None = None
    recovery_not_before: str = ""
    recovery_not_before_seq: int = 0
    task_id: int | None = None
    collection_scope_confirmation: bool = False
    confirmed_collection_member_ids: list[str] | None = None
    execution_intent: str = ""
    source_id: int | None = None
    queue_item_id: int | None = None

    def as_options(self) -> dict:
        data = asdict(self)
        data.pop("task_id", None)
        data.pop("queue_item_id", None)
        return data

    def snapshot(self) -> dict:
        """入队后不变的请求。保留 task_id，便于继续旧任务；不保留队列行号。"""
        data = asdict(self)
        data.pop("queue_item_id", None)
        return data


def collect_request_from_snapshot(data: dict) -> CollectRequest:
    allowed = {item.name for item in fields(CollectRequest)}
    cleaned = {key: value for key, value in dict(data or {}).items() if key in allowed}
    return CollectRequest(**cleaned)


def exports_user_files(request: CollectRequest) -> bool:
    """只有明确的「仅更新内容库」才不写用户导出文件。旧请求没有该字段时仍按原导出选项。"""
    return getattr(request, "execution_intent", "") != "library"


def listing_uses_incremental_stop(request: CollectRequest) -> bool:
    """更新内容库和更新并导出都扫完本次能列出的范围，不在第一页已知内容处停止。"""
    if getattr(request, "execution_intent", "") in {"library", "export"}:
        return False
    return bool(getattr(request, "update_only", False))


def library_image_directory(settings: Settings, record: ContentRecord) -> Path:
    token = f"{record.content_type}_{record.zhihu_content_id or 'item'}"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in token)[:80] or "item"
    return Path(settings.resolved_database_path()).parent / "library_images" / safe


class CollectionService:
    def __init__(self, settings: Settings, database: Database, controller: TaskController, on_progress=None, on_log=None):
        self.settings = settings
        self.database = database
        self.controller = controller
        self.on_progress = on_progress or (lambda progress: None)
        self.on_log = on_log or (lambda message: None)
        self.progress = Progress()
        self._started = time.time()
        self._body_saved_ids = set()
        self._failures = 0
        self._new_saved = 0
        self._updated_saved = 0
        self._skipped = 0
        self._failed_images = 0
        self._failed_exports = 0
        self._items_processed_this_run = 0
        self._hit_max_items = False
        self._listing_states = []
        self._body_failed_ids: set[str] = set()
        self._video_slots = 0
        self._video_fallbacks = 0
        self._video_answer_ids: set[str] = set()
        self._detail_page = None
        self._detail_listener = None
        self.task_id = 0

    def run(self, request: CollectRequest) -> str:
        self._started = time.time()
        self._request = request
        self._listing_states = []
        self._body_failed_ids = set()
        self._video_slots = 0
        self._video_fallbacks = 0
        self._video_answer_ids = set()
        if request.retry_content_ids:
            request.recovery_not_before = self.database.earliest_open_body_failure_time(request.retry_content_ids)
            request.recovery_not_before_seq = self.database.earliest_open_body_failure_seq(request.retry_content_ids)
        else:
            request.recovery_not_before = ""
            request.recovery_not_before_seq = 0
        parsed = parse_zhihu_url(request.url)
        mode = _resolve_mode(request.mode, parsed)
        self.mode = mode
        self._seen_by_type = {}
        self._totals_by_type = {}
        output_root = Path(request.output_dir) if request.output_dir else self.settings.resolved_output_dir()
        if exports_user_files(request):
            output_root.mkdir(parents=True, exist_ok=True)
        task_types = {
            "answers": "user_answers",
            "pins": "user_pins",
            "all": "user_all",
            "question": "question_answers",
            "single_answer": "single_answer",
            "collection": "collection_answers",
        }
        task_type = task_types.get(mode, "single_answer")
        if mode == "single_answer":
            target_id = parsed.answer_id or ""
        elif mode == "collection":
            target_id = parsed.collection_id or ""
        else:
            target_id = parsed.user_token or parsed.question_id or ""
        if request.task_id:
            self.task_id = request.task_id
            self.database.update_task(
                self.task_id,
                task_type=task_type,
                options_json=json.dumps(request.as_options(), ensure_ascii=False),
                status="running",
                error_message="",
                output_dir=str(output_root),
            )
        else:
            self.task_id = self.database.create_task(
                task_type=task_type,
                target_url=parsed.canonical,
                target_id=target_id,
                output_dir=str(output_root),
                options=request.as_options(),
            )
        if request.queue_item_id:
            from zhihu_collector.database.catalog import update_queue_item

            update_queue_item(self.database, int(request.queue_item_id), task_id=self.task_id)
        pdf = None
        downloader = None
        try:
            self._report("正在准备浏览器", found=0, completed=0)
            auth = AuthManager()
            headless = not self.settings.headful_debug
            pdf = PdfExporter() if exports_user_files(request) and request.export_pdf else None
            downloader = ImageDownloader(
                concurrency=self.settings.image_concurrency,
                attempts=self.settings.retry_count,
                base_delay_ms=self.settings.retry_base_delay_ms,
            ) if request.download_images else None
            with BrowserSession(headless=headless, storage_state=auth.state_path if auth.has_saved_state() else None, controller=self.controller) as session:
                if pdf is not None:
                    pdf.controller = self.controller
                    pdf.on_progress = lambda done, total: self._report(
                        f"正在生成 PDF（{done}/{total} 段）",
                        found=self.progress.found, completed=self.progress.completed)
                    from zhihu_collector.utils.task_diagnostics import pdf_stage_label
                    pdf.on_stage = lambda stage, done, total: self._report(
                        '正在生成 PDF：'+pdf_stage_label(stage,done,total),
                        found=self.progress.found, completed=self.progress.completed)
                if mode in {"answers", "all"}:
                    self._collect_user_kind(session, parsed, request, output_root, "answer", downloader, pdf)
                if mode in {"pins", "all"} and not self._hit_max_items:
                    self._collect_user_kind(session, parsed, request, output_root, "pin", downloader, pdf)
                if mode == "question":
                    self._collect_question(session, parsed, request, output_root, downloader, pdf)
                if mode == "single_answer":
                    self._collect_single_answer(session, parsed, request, output_root, downloader, pdf)
                if mode == "collection":
                    self._collect_collection(session, parsed, request, output_root, downloader, pdf)
                if mode != "collection":
                    if request.export_merged and exports_user_files(request):
                        self._report("正在生成合并文档", found=self.progress.found, completed=self.progress.completed)
                    self._write_merged(parsed, mode, request, output_root, pdf)
                self._report("正在关闭浏览器", found=self.progress.found, completed=self.progress.completed)

            reasons = []
            if self._failures:
                reasons.append(f"{self._failures} 条内容处理失败")
            if self._failed_images:
                reasons.append(f"{self._failed_images} 张图片下载失败")
            if self._failed_exports:
                reasons.append(f"{self._failed_exports} 个文件导出失败")
            has_processing_failures = bool(reasons)
            collection_gap = getattr(self, "_collection_gap", "")
            if mode == "collection" and collection_gap:
                reasons.append(collection_gap)
            notice = "" if mode == "collection" else self._listing_integrity_notice()

            task_status = "completed"
            if has_processing_failures:
                # completed 可能包含往期入库记录；本轮必须实际保存或复用过正文才算部分完成。
                has_success = bool(self._body_saved_ids or self._new_saved or self._updated_saved or self._skipped)
                task_status = "partial" if has_success else "failed"
                self.progress.integrity = task_status
                prefix = "搜集结束，但有失败项：" if has_success else "搜集失败："
                final_status = prefix + "，".join(reasons)
                if notice:
                    final_status += "；" + notice
                err_msg = final_status
            elif mode == "collection" and getattr(self, "_collection_status_text", ""):
                final_status = self._collection_status_text
                err_msg = "" if getattr(self, "_collection_ok", False) else final_status
                task_status = "completed" if getattr(self, "_collection_ok", False) else "partial"
            elif mode == "collection" and not getattr(self, "_collection_ok", True):
                final_status = "搜集结束，但有未完成项：" + "，".join(reasons or ["收藏夹结果不完整"])
                err_msg = final_status
                task_status = "partial"
            elif self._hit_max_items:
                task_status = "partial"
                self.progress.integrity = "partial"
                final_status = f"已达本次采集上限（{request.max_items} 篇），不是全量归档"
                err_msg = ""
            elif notice:
                final_status = notice
                err_msg = notice
                task_status = "unconfirmed"
                self.progress.integrity = "unconfirmed"
            else:
                final_status = success_summary(self._new_saved, self._updated_saved, self._skipped)
                err_msg = ""
                self.progress.integrity = "complete"
            video_note = self._video_note()
            if video_note:
                final_status = final_status + video_note
            self._write_listing_summary()
            scanned, declared = self._listing_progress_totals()
            if scanned is not None:
                self.progress.found = scanned
            if declared is not None:
                self.progress.declared_total = declared

            self.database.update_task(
                self.task_id,
                status=task_status,
                total_found=self.progress.found,
                total_completed=self.progress.completed,
                error_message=err_msg,
            )
            self._report(
                final_status,
                found=self.progress.found,
                completed=self.progress.completed,
                scanned=scanned,
                declared=declared,
            )
            return str(output_root)
        except TaskStopped:
            self.database.update_task(self.task_id, status="interrupted", total_found=self.progress.found, total_completed=self.progress.completed, error_message="任务已停止，可以稍后继续。")
            self._report("已停止", found=self.progress.found, completed=self.progress.completed)
            raise
        except CollectionScopeReady:
            self.database.update_task(
                self.task_id,
                status="awaiting_confirmation",
                total_found=self.progress.found,
                total_completed=self.progress.completed,
                error_message="",
            )
            self._report("收藏夹清单已读取，请确认导出范围", found=self.progress.found, completed=self.progress.completed)
            raise
        except LoginExpiredError as exc:
            self.database.update_task(self.task_id, status="needs_login", error_message=str(exc), total_found=self.progress.found, total_completed=self.progress.completed)
            self._report(str(exc), found=self.progress.found, completed=self.progress.completed)
            raise
        except VerificationRequiredError as exc:
            self.database.update_task(self.task_id, status="needs_verification", error_message=str(exc), total_found=self.progress.found, total_completed=self.progress.completed)
            self._report(str(exc), found=self.progress.found, completed=self.progress.completed)
            raise
        except Exception as exc:
            logger.exception("任务失败")
            message = self._failure_message(exc)
            self.database.update_task(self.task_id, status="failed", error_message=message, total_found=self.progress.found, total_completed=self.progress.completed)
            self._report(message, found=self.progress.found, completed=self.progress.completed)
            if mode == "single_answer" and parsed.answer_id and parsed.answer_id not in self._body_saved_ids:
                failed_record = ContentRecord(zhihu_content_id=parsed.answer_id, content_type='answer', url=parsed.canonical)
                self.database.record_content_failure(failed_record, self.task_id, request.as_options(), message)
            raise CollectorError(message) from exc
        finally:
            self._note_queue_failures(request)
            self._note_source(request)
            if self._detail_page is not None:
                try:
                    self._detail_page.close()
                except Exception:
                    pass
            if pdf is not None:
                pdf.close()
            if downloader is not None:
                downloader.close()

    def _collect_user_kind(self, session, parsed, request, output_root, content_type, downloader, pdf) -> None:
        if content_type == "answer":
            start_url = parsed.answers_page_url
            parse_fn = lambda payloads, html: records_from_page_sources(payloads=payloads, html=html, user_token=parsed.user_token or "")
        else:
            start_url = parsed.pins_page_url
            parse_fn = parse_pin_page
        author_url = public_user_url(parsed.user_kind, parsed.user_token)
        known = self.database.known_versions(author_url=author_url, content_type=content_type)
        if request.retry_content_ids:
            for content_id in request.retry_content_ids:
                known.pop(content_id, None)
        folder_ready = {"path": None}
        saved_rows = self.database.list_contents(author_url=author_url, content_type=content_type)
        if saved_rows and not request.retry_content_ids and exports_user_files(request):
            folder_ready["path"] = _author_folder(
                output_root,
                next((row["author_name"] for row in saved_rows if row["author_name"]), "") or (parsed.user_token or ""),
                parsed.user_token or "",
            )
            self._failures += export_saved_rows(
                self.database,
                saved_rows,
                folder_ready["path"],
                request,
                self.settings,
                pdf if request.export_pdf else None,
                downloader,
                controller=self.controller,
            )

        def on_page(records, totals, seen, skipped=0) -> None:
            if skipped:
                self._skipped += skipped
                self._log(f"跳过 {skipped} 条未变化内容")
            for record in records:
                if request.retry_content_ids and record.zhihu_content_id not in request.retry_content_ids:
                    continue
                if request.max_items > 0 and self._items_processed_this_run >= request.max_items:
                    self._hit_max_items = True
                    raise LimitReached()
                self.controller.checkpoint()
                try:
                    if not record.author.url_token:
                        record.author.url_token = parsed.user_token or ""
                        record.author.url = author_url
                    self._enrich(session, record)
                    if getattr(record, "content_incomplete", False):
                        raise CollectorError(f"未能补全正文（ID: {record.zhihu_content_id}）：详情内容仍不完整或未找到")
                    root = folder_ready["path"]
                    if root is None:
                        root = _author_folder(output_root, record.author.name, parsed.user_token or "")
                        folder_ready["path"] = root
                    self._persist_record(record, root, request, downloader, pdf, index_scope="author")
                except (LoginExpiredError, VerificationRequiredError, TaskStopped, LimitReached):
                    raise
                except Exception as exc:
                    self._failures += 1
                    self._body_failed_ids.add(record.zhihu_content_id)
                    message = self._failure_message(exc)
                    if record.content_incomplete or not _body_is_usable(record):
                        self.database.record_content_failure(record, self.task_id, request.as_options(), message)
                    logger.exception("跳过一条失败内容 %s", record.url)
                    self._log(f"跳过一条内容：{record.url or record.zhihu_content_id}（{message}）")
                    if isinstance(exc, BrowserOperationTimeout):
                        raise  # The watchdog has retired this session; do not poison later rows.
                finally:
                    self._items_processed_this_run += 1
                    completed = self._completed_count(author_url=author_url, content_type=content_type)
                    found = self._found_count(content_type, totals, seen, completed)
                    self.progress.found = found
                    self.progress.completed = completed
                    self.database.update_task(self.task_id, total_found=found, total_completed=completed)

                    self._report("正在搜集", found=found, completed=completed,
                                 current=record.question_title or record.title, scanned=seen,
                                 declared=totals if isinstance(totals, int) else None)

                if request.max_items > 0 and self._items_processed_this_run >= request.max_items:
                    self._hit_max_items = True
                    raise LimitReached()
            completed = self._completed_count(author_url=author_url, content_type=content_type)
            found = self._found_count(content_type, totals, seen, completed)
            title = records[0].question_title or records[0].title if records else self.progress.current_title
            self._report(
                "正在搜集",
                found=found,
                completed=completed,
                current=title,
                downloader=downloader,
                scanned=seen,
                declared=totals if isinstance(totals, int) else None,
            )
            self.database.update_task(self.task_id, total_found=found, total_completed=completed)

        state_box: dict = {}
        list_target = ListTarget(
            "member_answers" if content_type == "answer" else "member_pins",
            parsed.user_token or "",
        )
        try:
            collect_listing(
                session,
                start_url,
                settings=self.settings,
                controller=self.controller,
                parse_fn=parse_fn,
                on_page=on_page,
                known_versions=known,
                update_only=listing_uses_incremental_stop(request),
                list_target=list_target,
                state_box=state_box,
                on_diagnostic=self._record_listing_page,
            )
        except LimitReached:
            logger.info("已达到设定采集上限（%d 篇）", request.max_items)
        finally:
            state = state_box.get("state")
            if state is not None:
                state.saved_total = self.database.count_contents(author_url=author_url, content_type=content_type)
                self._listing_states.append(state)
            completed = self._completed_count(author_url=author_url, content_type=content_type)
            found = self._seen_by_type.get(content_type, self.progress.found)
            self.progress.found = found
            self.progress.completed = completed
            self.database.update_task(self.task_id, total_found=found, total_completed=completed)

    def _collect_question(self, session, parsed, request, output_root, downloader, pdf) -> None:
        question_id = parsed.question_id or ""
        if not question_id:
            raise UnsupportedUrlError(request.url)
        known = self.database.known_versions(question_zhihu_id=question_id, content_type="answer")
        if request.retry_content_ids:
            for content_id in request.retry_content_ids:
                known.pop(content_id, None)
        folder = {"path": None}
        saved_rows = self.database.list_contents(question_zhihu_id=question_id, content_type="answer")
        if saved_rows and not request.retry_content_ids and exports_user_files(request):
            title = saved_rows[0]["title"] or question_id
            folder["path"] = output_root / safe_filename(f"问题-{question_id}-{title}", max_length=60)
            self._failures += export_saved_rows(
                self.database,
                saved_rows,
                folder["path"],
                request,
                self.settings,
                pdf if request.export_pdf else None,
                downloader,
                controller=self.controller,
            )

        def on_page(records, totals, seen, skipped=0) -> None:
            if skipped:
                self._skipped += skipped
                self._log(f"跳过 {skipped} 条未变化内容")
            for record in records:
                if request.retry_content_ids and record.zhihu_content_id not in request.retry_content_ids:
                    continue
                if request.max_items > 0 and self._items_processed_this_run >= request.max_items:
                    self._hit_max_items = True
                    raise LimitReached()
                self.controller.checkpoint()
                try:
                    if not record.question_id:
                        record.question_id = question_id
                    self._enrich(session, record)
                    if getattr(record, "content_incomplete", False):
                        raise CollectorError(f"未能补全正文（ID: {record.zhihu_content_id}）：详情内容仍不完整或未找到")
                    if folder["path"] is None:
                        title = record.question_title or question_id
                        folder["path"] = output_root / safe_filename(f"问题-{question_id}-{title}", max_length=60)
                    self._persist_record(record, folder["path"], request, downloader, pdf, index_scope="question")
                except (LoginExpiredError, VerificationRequiredError, TaskStopped, LimitReached):
                    raise
                except Exception as exc:
                    self._failures += 1
                    self._body_failed_ids.add(record.zhihu_content_id)
                    message = self._failure_message(exc)
                    if record.content_incomplete or not _body_is_usable(record):
                        self.database.record_content_failure(record, self.task_id, request.as_options(), message)
                    logger.exception("跳过一条失败回答 %s", record.url)
                    self._log(f"跳过一条回答：{record.url}（{message}）")
                    if isinstance(exc, BrowserOperationTimeout):
                        raise
                finally:
                    self._items_processed_this_run += 1
                    completed = self.database.count_contents(question_zhihu_id=question_id, content_type="answer")
                    found = self._found_count("answer", totals, seen, completed)
                    self.progress.found = found
                    self.progress.completed = completed
                    self.database.update_task(self.task_id, total_found=found, total_completed=completed)

                if request.max_items > 0 and self._items_processed_this_run >= request.max_items:
                    self._hit_max_items = True
                    raise LimitReached()
            completed = self.database.count_contents(question_zhihu_id=question_id, content_type="answer")
            found = self._found_count("answer", totals, seen, completed)
            current = records[0].question_title if records else self.progress.current_title
            self._report(
                "正在搜集",
                found=found,
                completed=completed,
                current=current,
                downloader=downloader,
                scanned=seen,
                declared=totals if isinstance(totals, int) else None,
            )
            self.database.update_task(self.task_id, total_found=found, total_completed=completed)

        state_box: dict = {}
        try:
            collect_listing(
                session,
                parsed.question_page_url,
                settings=self.settings,
                controller=self.controller,
                parse_fn=lambda payloads, html: parse_question_page(payloads, html, question_id=question_id),
                on_page=on_page,
                known_versions=known,
                update_only=listing_uses_incremental_stop(request),
                list_target=ListTarget("question_answers", question_id),
                state_box=state_box,
                on_diagnostic=self._record_listing_page,
            )
        except LimitReached:
            logger.info("已达到设定采集上限（%d 篇）", request.max_items)
        finally:
            state = state_box.get("state")
            if state is not None:
                state.saved_total = self.database.count_contents(question_zhihu_id=question_id, content_type="answer")
                self._listing_states.append(state)
            completed = self.database.count_contents(question_zhihu_id=question_id, content_type="answer")
            found = self._seen_by_type.get("answer", self.progress.found)
            self.progress.found = found
            self.progress.completed = completed
            self.database.update_task(self.task_id, total_found=found, total_completed=completed)

    def _collect_single_answer(self, session, parsed, request, output_root, downloader, pdf) -> None:
        answer_id = parsed.answer_id or ""
        if not answer_id:
            raise UnsupportedUrlError(request.url)
        start_url = parsed.answer_page_url or parsed.canonical or request.url
        self._report("正在获取回答", found=1, completed=0, current=start_url)
        page = session.new_page()
        listener = NetworkListener()
        listener.attach(page)
        try:
            self.controller.checkpoint()
            navigation_once(page, start_url)
            if "/question/" not in page.url:
                try:
                    page.wait_for_url(re.compile(rf"/question/\d+/answer/{answer_id}"), timeout=6000)
                except Exception:
                    pass
            page.wait_for_timeout(max(500, self.settings.request_interval_ms))
            page_html = self._read_answer_html(page, listener, answer_id)
            final_url = page.url
            redirected_parsed = parse_zhihu_url(final_url)
            if redirected_parsed.kind == "answer" and redirected_parsed.question_id and not parsed.question_id:
                parsed = redirected_parsed
            details = records_from_page_sources(
                payloads=listener.snapshot_data(),
                html=page_html,
                user_token="",
            )
            inspect_page(page, found_content=len(details), network_errors=listener.consume_errors())
            matched = next((item for item in details if item.zhihu_content_id == answer_id), None)
            if matched is None:
                dom_records = parse_answer_cards_html(page_html)
                matched = next((item for item in dom_records if item.zhihu_content_id == answer_id), None)
            if matched is None and page_html:
                soup = BeautifulSoup(page_html, "lxml")
                target_node = answer_container(soup, answer_id)
                if target_node:
                    title_node = soup.select_one(".QuestionHeader-title, h1")
                    title = title_node.get_text(" ", strip=True) if title_node else "知乎回答"
                    author_node = target_node.select_one(".AuthorInfo-name, [itemprop='name']")
                    author_name = author_node.get_text(" ", strip=True) if author_node else "知乎答主"
                    rt = target_node.select_one(".RichContent .RichText, .RichText")
                    rt_html = rt.decode_contents().strip() if rt else ""
                    is_coll = bool(target_node.select(".is-collapsed, .ContentItem-more"))
                    matched = ContentRecord(
                        zhihu_content_id=answer_id,
                        content_type="answer",
                        url=final_url,
                        title=title,
                        author=AuthorInfo(name=author_name),
                        question_id=parsed.question_id or "",
                        question_title=title,
                        question_url=parsed.question_page_url or "",
                        html_content=rt_html,
                        content_incomplete=(not rt_html) or is_coll,
                    )

            if matched is None:
                raise CollectorError(f"未找到目标回答（ID: {answer_id}）")
            if matched.content_incomplete or not matched.html_content:
                self._enrich(session, matched)
            if matched.content_incomplete or not matched.html_content:
                raise CollectorError(f"未能补全目标回答正文（ID: {answer_id}）：未找到完整正文")
            title = matched.question_title or matched.title or answer_id
            folder = output_root / safe_filename(f"回答-{answer_id}-{title}", max_length=60)
            self._persist_record(matched, folder, request, downloader, pdf, index_scope="single")
            self._items_processed_this_run += 1
            self._report("正在搜集", found=1, completed=1, current=matched.title)
            self.database.update_task(self.task_id, total_found=1, total_completed=1)
        finally:
            page.close()

    def _persist_record(
        self,
        record: ContentRecord,
        root: Path,
        request: CollectRequest,
        downloader,
        pdf,
        *,
        index_scope: str,
        preserve_existing_images: bool = False,
    ) -> str:
        if getattr(record, "content_incomplete", False) or not _body_is_usable(record):
            raise CollectorError(f"正文内容不完整（ID: {record.zhihu_content_id}），未获取到完整正文，不能作为成功记录保存")
        if not exports_user_files(request):
            root = library_image_directory(self.settings, record)
        source_html = record.html_content or (f"<p>{html.escape(record.excerpt)}</p>" if record.excerpt else "")
        cleaned = clean_content(source_html, source_url=record.url or "")
        if cleaned.video_count:
            self._video_slots += cleaned.video_count
            self._video_fallbacks += cleaned.video_fallback_count
            self._video_answer_ids.add(record.zhihu_content_id)
        image_dir = root / "images"
        image_urls = list(dict.fromkeys([*cleaned.image_urls, *(record.image_urls or [])]))
        images = []
        if downloader is not None and image_urls:
            try:
                images = [
                    item.as_row()
                    for item in downloader.download_all(
                        image_urls,
                        image_dir,
                        f"{record.content_type}_{record.zhihu_content_id}",
                    )
                ]
            except (LoginExpiredError, VerificationRequiredError, TaskStopped):
                raise
            except Exception as exc:
                images = [
                    {
                        "original_url": url,
                        "local_path": "",
                        "download_status": "failed",
                        "hash": "",
                        "sort_index": index,
                        "error": str(exc),
                    }
                    for index, url in enumerate(image_urls, start=1)
                ]
        body = cleaned.markdown or html_to_markdown(cleaned.html)
        action, local_id = self.database.save_content(
            record,
            markdown=body,
            html=cleaned.html or source_html,
            index_scope=index_scope,
        )
        self._body_saved_ids.add(record.zhihu_content_id)
        self.database.resolve_content_failure(record.content_type, record.zhihu_content_id)
        if action in ("inserted", "saved"):
            self._new_saved += 1
            outcome = "inserted"
        elif action == "updated":
            self._updated_saved += 1
            outcome = "updated"
        elif action == "metadata":
            self._skipped += 1
            outcome = "metadata"
        else:
            self._skipped += 1
            outcome = "reused"
        from zhihu_collector.database.catalog import record_task_content

        record_task_content(self.database, self.task_id, local_id, outcome)
        images = [_normalize_stored_image(row) for row in images]
        existing_images = [dict(image) for image in self.database.list_images(local_id)]
        attempted_urls = {row.get("original_url") or "" for row in images}
        if action in {"skipped", "metadata"}:
            if downloader is not None:
                row_for_images = self.database.get_content(local_id)
                images = retry_failed_images(self.database, row_for_images, image_dir, downloader)
            else:
                images = existing_images
        else:
            images = _merge_image_rows(
                existing_images,
                images,
                keep_existing=bool(preserve_existing_images and not images),
            )
            self.database.replace_images(local_id, images)
        self._failed_images += _image_failure_count(
            [row for row in images if (row.get("original_url") or "") in attempted_urls]
        )
        self._log(f"{'跳过未变化' if action in {'skipped', 'metadata'} else '已保存'}：{record.question_title or record.title or record.zhihu_content_id}")
        if not exports_user_files(request) or not request.export_separate:
            return action
        row = self.database.get_content(local_id)
        kind_dir = root / ("想法" if record.content_type == "pin" else "回答")
        results = write_separate_exports(
            row,
            images,
            kind_dir,
            request,
            self.settings,
            pdf if request.export_pdf else None,
            self.database,
        )
        failed = [f"{name}: {message}" for name, message in results.items() if message not in {"success", "skipped", "kept"}]
        if failed:
            self._failed_exports += len(failed)
            raise RuntimeError("导出未完成：" + "；".join(failed))
        if action == "updated" and any(msg == "skipped" for msg in results.values()):
            self._log(f"内容库已更新，已有导出文件已跳过：{record.question_title or record.title or record.zhihu_content_id}")
        return action

    def _read_answer_html(self, page, listener, answer_id: str, user_token: str = "") -> str:
        def ready(page_html):
            records = records_from_page_sources(
                payloads=listener.snapshot_data(), html=page_html, user_token=user_token,
            )
            if any(
                item.zhihu_content_id == answer_id and item.html_content and not item.content_incomplete
                for item in records
            ):
                return True
            if not isinstance(page_html, str):
                return False
            soup = BeautifulSoup(page_html, "lxml")
            target = answer_container(soup, answer_id)
            if target is None or target.select(".is-collapsed, .ContentItem-more"):
                return False
            body = target.select_one(".RichContent .RichText, .RichText")
            return bool(body and body.decode_contents().strip())

        return read_page_content(
            page, self.controller, network_errors=listener.errors,
            is_ready=ready, attempts=40, timeout_ms=20000,
        )

    def _enrich(self, session, record: ContentRecord) -> None:
        if not record.content_incomplete and _body_is_usable(record):
            return
        record.content_incomplete = True
        if not record.url:
            return
        self.controller.checkpoint()
        self._log(f"补全正文：{record.url}")
        if self._detail_page is None:
            self._detail_page = session.new_page()
            self._detail_listener = NetworkListener()
            self._detail_listener.attach(self._detail_page)
        page = self._detail_page
        listener = self._detail_listener
        listener.clear()
        page.bring_to_front()
        navigation_once(page, record.url)
        page.wait_for_timeout(max(400, self.settings.request_interval_ms))
        if record.content_type == "pin":
            self._enrich_pin(page, listener, record)
            return
        try:
            page_html = self._read_answer_html(page, listener, record.zhihu_content_id, record.author.url_token)
        except PageContentNotReady:
            logger.warning("等待正文超时（ID: %s），保留未补全状态", record.zhihu_content_id)
            return
        details = records_from_page_sources(
            payloads=listener.snapshot_data(),
            html=page_html,
            user_token=record.author.url_token,
        )
        inspect_page(page, found_content=len(details), network_errors=listener.errors)
        matched = next((item for item in details if item.zhihu_content_id == record.zhihu_content_id and item.html_content), None)
        if matched is not None and not matched.content_incomplete:
            record.html_content = matched.html_content
            record.image_urls = matched.image_urls or record.image_urls
            record.content_incomplete = False
            if matched.vote_count is not None:
                record.vote_count = matched.vote_count
            if matched.author.name and not record.author.name:
                record.author = matched.author
            return
        if isinstance(page_html, str) and page_html.strip():
            soup = BeautifulSoup(page_html, "lxml")
            target_node = answer_container(soup, record.zhihu_content_id)
            if target_node:
                rt = target_node.select_one(".RichContent .RichText, .RichText")
                if rt and rt.decode_contents().strip():
                    if not target_node.select(".is-collapsed, .ContentItem-more"):
                        record.html_content = rt.decode_contents().strip()
                        record.content_incomplete = False
                        author_node = target_node.select_one(".AuthorInfo-name, [itemprop='name']")
                        if author_node and not record.author.name:
                            record.author.name = author_node.get_text(" ", strip=True)
                        return
        logger.warning("未能补全正文（ID: %s）：未在详情页找到匹配内容", record.zhihu_content_id)

    @serialized_export
    def _enrich_pin(self, page, listener, record: ContentRecord) -> None:
        """详情补全只接受目标想法，推荐流或回答正文不能替代它。"""
        match = None

        def ready(page_html):
            nonlocal match
            payloads = [*listener.snapshot_data(), *extract_embedded_payloads(page_html)]
            match = next((
                pin for pin in parse_pin_payloads(payloads)
                if pin.zhihu_content_id == record.zhihu_content_id
                and not pin.content_incomplete and _body_is_usable(pin)
            ), None)
            return match is not None

        try:
            read_page_content(page, self.controller, network_errors=listener.errors,
                              is_ready=ready, attempts=20, timeout_ms=10_000)
        except PageContentNotReady:
            logger.warning("未取得可用的想法正文（ID: %s）", record.zhihu_content_id)
            return
        if match is not None:
            record.html_content = match.html_content
            record.image_urls = match.image_urls or record.image_urls
            record.title = record.title or match.title
            record.content_incomplete = False

    def _merged_scope_label(self, label: str) -> str:
        if self._hit_max_items:
            return f"{label}（本次上限，不是全量）"
        for state in self._listing_states:
            declared = int(getattr(state, "declared_total", None) or 0)
            scanned = len(getattr(state, "seen_ids", set()) or [])
            if declared and scanned < declared:
                return f"{label}（清单未确认，不是全量）"
        return label

    def _note_queue_failures(self, request: CollectRequest) -> None:
        """把实际失败计数交给队列项；队列结束时据此显示失败数，不按参考总数推算。"""
        if not request.queue_item_id:
            return
        from zhihu_collector.database.catalog import update_queue_item

        update_queue_item(
            self.database,
            int(request.queue_item_id),
            result_json=json.dumps(
                {
                    "failed": self._failures,
                    "images_failed": self._failed_images,
                    "exports_failed": self._failed_exports,
                }
            ),
        )

    def _note_source(self, request: CollectRequest) -> None:
        source_id = getattr(request, "source_id", None)
        if not source_id or not self.task_id:
            return
        from zhihu_collector.database.catalog import note_source_result

        task = self.database.get_task(self.task_id)
        status = task["status"] if task is not None else "failed"
        integrity = self.progress.integrity or ""
        if integrity == "unconfirmed" or status == "unconfirmed":
            shown = "unconfirmed"
            succeeded = False
        elif status in {"failed", "needs_login", "needs_verification", "interrupted", "awaiting_confirmation"}:
            shown = {
                "needs_verification": "needs_verify",
                "awaiting_confirmation": "awaiting_confirm",
            }.get(status, status)
            succeeded = False
        elif integrity == "partial" or status == "partial" or self._failures or self._failed_images or self._failed_exports:
            shown = "partial"
            succeeded = False
        else:
            shown = "completed"
            succeeded = True
        note_source_result(
            self.database,
            int(source_id),
            status=shown,
            task_id=self.task_id,
            succeeded=succeeded,
            learned_name=self._learned_source_name(request),
            result={
                "saved_new": self._new_saved,
                "updated": self._updated_saved,
                "body_updated": self._updated_saved,
                "reused": self._skipped,
                "failed": self._failures,
                "images_failed": self._failed_images,
                "exports_failed": self._failed_exports,
                "scanned": self.progress.scanned,
                "declared_total": self.progress.declared_total,
                "integrity": integrity or shown,
            },
        )

    def _learned_source_name(self, request: CollectRequest) -> str:
        from zhihu_collector.database.catalog import fetch_source

        source = fetch_source(self.database, int(request.source_id))
        if source is None:
            return ""
        if source["target_kind"] == "collection":
            collection_id = str(source["identity_key"]).split(":", 1)[-1]
            row = self.database.get_collection(collection_id)
            title = (row["title"] if row is not None else "") or ""
            if title and not title.startswith("http"):
                return title
            return ""
        if source["target_kind"] != "user":
            return ""
        rows = self.database.list_contents(author_url=source["canonical_url"])
        names = {row["author_name"] for row in rows if row["author_name"]}
        if len(names) == 1:
            return next(iter(names))
        return ""

    @serialized_export
    def _write_merged(self, parsed, mode, request: CollectRequest, output_root: Path, pdf: PdfExporter) -> None:
        if not request.export_merged or not exports_user_files(request):
            return
        groups = []
        if mode in {"answers", "all"} and parsed.kind == "user":
            groups.append(("answer", self._merged_scope_label("全部回答")))
        if mode in {"pins", "all"} and parsed.kind == "user":
            groups.append(("pin", self._merged_scope_label("全部想法")))
        if mode == "question":
            groups.append(("answer", self._merged_scope_label("全部回答")))
        if mode == "single_answer":
            groups.append(("answer", "回答"))
        for content_type, label in groups:
            if parsed.kind == "user":
                rows = self.database.list_contents(author_url=public_user_url(parsed.user_kind, parsed.user_token), content_type=content_type)
                author = rows[0]["author_name"] if rows else (parsed.user_token or "")
                folder = _author_folder(output_root, author, parsed.user_token or "")
                title = f"{author}{label}"
            elif parsed.kind == "question":
                rows = self.database.list_contents(question_zhihu_id=parsed.question_id or "", content_type="answer")
                author = "多位作者"
                question_title = rows[0]["title"] if rows else parsed.question_id
                folder = output_root / safe_filename(f"问题-{parsed.question_id}-{question_title}", max_length=60)
                title = f"{question_title}{label}"
            else:
                rows = self.database.list_contents(zhihu_content_id=parsed.answer_id or "", content_type="answer")
                author = rows[0]["author_name"] if rows else "知乎作者"
                question_title = rows[0]["title"] if rows else (parsed.answer_id or "回答")
                folder = output_root / safe_filename(f"回答-{parsed.answer_id}-{question_title}", max_length=60)
                title = f"{question_title}-回答"
            if not rows:
                continue
            destination = folder / safe_filename(title, extension=".md", max_length=80)
            merged, pending = render_merged_rows(
                self.database, rows, destination, title=title, author=author,
                save_metadata=request.save_metadata, controller=self.controller,
            )
            folder.mkdir(parents=True, exist_ok=True)
            scope_json = merged_scope_json(
                title=title, author=author, rows=rows, request=request,
                anchor_field="export_index", time_field="created", pending=pending,
            )
            formats = _requested_formats(request)
            if request.conflict_policy == "new_copy":
                base = destination.stem
                copy_index = 1
                while any(destination.with_suffix(suffix).exists() for _, suffix in formats):
                    destination = folder / f"{base}_副本{copy_index}.md"
                    copy_index += 1
            for export_format, suffix in formats:
                self.controller.checkpoint()
                target = destination if suffix == ".md" else destination.with_suffix(suffix)
                if request.conflict_policy == "skip" and target.exists():
                    continue
                primary_content_id = rows[0]["id"] if rows else 0
                if recovery_keeps_existing_target(self.database, target, export_format, request.recovery_not_before, request.recovery_not_before_seq):
                    self._log(f"正文恢复未覆盖后续导出：{target.name}")
                    continue
                try:
                    _write_export_file(export_format, merged, folder, target, pdf, title)
                    self.database.record_export(
                        primary_content_id,
                        export_format=export_format,
                        output_path=str(target.resolve()),
                        content_hash=sha256_text(merged),
                        status="success",
                        fingerprint=sha256_text(merged),
                        export_type="merged",
                        scope_json=scope_json,
                    )
                except TaskStopped:
                    raise
                except Exception as exc:
                    logger.exception("合并导出 %s 失败：%s", target, exc)
                    self._failures += 1
                    self._failed_exports += 1
                    self._log(f"合并导出 {export_format} 失败：{exc}")
                    self.database.record_export(
                        primary_content_id,
                        export_format=export_format,
                        output_path=str(target.resolve()),
                        content_hash="",
                        status="failed",
                        error_message=str(exc),
                        export_type="merged",
                        scope_json=scope_json,
                    )

    def _collect_collection(self, session, parsed, request: CollectRequest, output_root: Path, downloader, pdf) -> None:
        from zhihu_collector.collectors.collection_answers import collect_collection_manifest

        collection_id = parsed.collection_id or ""
        manifest = collect_collection_manifest(
            session,
            collection_id,
            self.settings,
            self.controller,
            on_progress=lambda found, total, msg: self._report(msg, found=found, completed=self.progress.completed),
            on_log=self._log,
            max_items=request.max_items,
        )

        self.database.save_collection(manifest.collection_id, manifest.title, manifest.url)
        members = [
            {
                "answer_id": ans.zhihu_content_id,
                "sort_order": idx + 1,
                "item_status": "discovered",
            }
            for idx, ans in enumerate(manifest.answers)
        ]
        self.database.save_collection_members(manifest.collection_id, members)
        answer_map = {ans.zhihu_content_id: ans for ans in manifest.answers}
        if request.retry_content_ids:
            allowed = set(request.confirmed_collection_member_ids) if request.confirmed_collection_member_ids is not None else set(answer_map)
            answers_to_export = []
            for answer_id in request.retry_content_ids:
                if answer_id not in allowed or answer_id not in answer_map:
                    self._failures += 1
                    self._log(f"正文重试未扩大范围，回答 {answer_id} 不在原确认快照中或当前清单没有该回答")
                    continue
                answers_to_export.append(answer_map[answer_id])
            merge_order_ids = list(request.confirmed_collection_member_ids or [ans.zhihu_content_id for ans in manifest.answers])
        elif request.confirmed_collection_member_ids is not None:
            missing = [answer_id for answer_id in request.confirmed_collection_member_ids if answer_id not in answer_map]
            if missing:
                raise CollectorError("确认范围中的部分回答已经不存在，请重新读取收藏夹清单后确认。")
            answers_to_export = [answer_map[answer_id] for answer_id in request.confirmed_collection_member_ids]
            merge_order_ids = list(request.confirmed_collection_member_ids)
        else:
            answers_to_export = list(manifest.answers)
            merge_order_ids = [ans.zhihu_content_id for ans in answers_to_export]

        if (
            not request.retry_content_ids
            and request.collection_scope_confirmation
            and request.confirmed_collection_member_ids is None
            and manifest.answers
            and not manifest.interrupted
        ):
            plan = ExportPlan(
                scope_kind="collection_exact",
                source_title=manifest.title,
                source_url=manifest.url,
                source_id=manifest.collection_id,
                member_ids=[ans.zhihu_content_id for ans in manifest.answers],
                member_items=[
                    {
                        "id": ans.zhihu_content_id,
                        "title": ans.title or ans.question_title or "未命名",
                        "author": ans.author.name or "未知作者",
                        "status": "待确认",
                    }
                    for ans in manifest.answers
                ],
                export_markdown=request.export_markdown,
                export_docx=request.export_docx,
                export_pdf=request.export_pdf,
                export_separate=request.export_separate,
                export_merged=request.export_merged,
                download_images=request.download_images,
                save_metadata=request.save_metadata,
                output_root=str(output_root),
                conflict_policy=request.conflict_policy,
                max_items=request.max_items,
                collection_snapshot_id=None,
                merged_filename=manifest.title if request.export_merged else "",
                expected_file_count=expected_export_file_count(
                    len(manifest.answers),
                    export_markdown=request.export_markdown,
                    export_docx=request.export_docx,
                    export_pdf=request.export_pdf,
                    export_separate=request.export_separate,
                    export_merged=request.export_merged,
                ),
                target_count_unknown=False,
            )
            self.pending_collection_plan = plan
            raise CollectionScopeReady(plan)

        snapshot_member_ids = list(merge_order_ids)
        snapshot_id = self.database.create_collection_snapshot(
            self.task_id,
            manifest.collection_id,
            snapshot_member_ids,
            total_declared=manifest.total_declared,
            discovered_answers=len(manifest.answers),
            non_answer_count=manifest.non_answer_count,
            duplicate_count=manifest.duplicate_count,
            integrity_status=manifest.integrity_status,
            finish_reason=manifest.finish_reason,
        )

        safe_coll_title = safe_filename(manifest.title, max_length=60)
        collection_dir = output_root / "收藏夹" / f"{safe_coll_title}-{collection_id}"
        if exports_user_files(request):
            collection_dir.mkdir(parents=True, exist_ok=True)

        self._report("正在获取回答正文并导出", found=len(answers_to_export), completed=0)
        results_log: list[dict] = []
        if manifest.interrupted:
            self._finish_collection_run(
                manifest, results_log, answers_to_export, merge_order_ids, collection_dir, request, pdf, snapshot_id
            )
            if manifest.interrupted == "stopped":
                raise TaskStopped()
            if manifest.interrupted == "verification":
                raise VerificationRequiredError(manifest.finish_reason or "知乎要求进行安全验证。")
            raise LoginExpiredError(manifest.finish_reason or "知乎登录状态已失效，请重新登录。")

        try:
            for idx, ans in enumerate(answers_to_export, start=1):
                self.controller.checkpoint()
                self.progress.current_title = ans.question_title or ans.title
                results_log.append(self._export_collection_answer(session, ans, request, collection_dir, downloader, pdf))
                self._report("正在获取回答正文并导出", found=len(answers_to_export), completed=idx, current=ans.title)
        except (TaskStopped, LoginExpiredError, VerificationRequiredError):
            self._finish_collection_run(
                manifest, results_log, answers_to_export, merge_order_ids, collection_dir, request, pdf, snapshot_id,
                interrupted=True,
            )
            raise

        merge_note = ""
        if request.export_merged and exports_user_files(request):
            self._report("正在生成合并文档", found=len(answers_to_export), completed=len(results_log))
            merge_rows = self._rows_in_order(merge_order_ids)
            if merge_rows is None:
                self._failed_exports += 1
                merge_note = "合并范围不完整，已保留旧合并文件"
                self._log(merge_note)
            else:
                merge_failed, merge_skipped = self._write_collection_merged(manifest, merge_rows, collection_dir, request, pdf)
                if merge_failed:
                    merge_note = "合并文件有格式失败"
                    for item in results_log:
                        if item.get("status") == "已保存":
                            item["status"] = "合并导出失败"
                elif merge_skipped and not request.export_separate:
                    for item in results_log:
                        if item.get("status") == "已保存":
                            item["status"] = "跳过"
                else:
                    for item in results_log:
                        if item.get("status") == "已保存":
                            item["status"] = "成功"
        self._finish_collection_run(
            manifest, results_log, answers_to_export, merge_order_ids, collection_dir, request, pdf, snapshot_id,
            merge_note=merge_note,
        )

    def _export_collection_answer(self, session, ans, request: CollectRequest, collection_dir: Path, downloader, pdf) -> dict:
        from zhihu_collector.parsers.collection_parser import answer_body_needs_enrichment

        ans_id = ans.zhihu_content_id
        author_name = ans.author.name
        existing_rows = self.database.list_contents(zhihu_content_id=ans_id, content_type="answer")
        existing_row = existing_rows[0] if existing_rows else None
        reused = _can_reuse_saved_answer(existing_row, ans)
        if reused and existing_row is not None:
            ans.html_content = existing_row["html_content"]
            ans.content_incomplete = False
            if not ans.author.name and existing_row["author_name"]:
                ans.author.name = existing_row["author_name"]
        else:
            try:
                self._enrich(session, ans)
            except (TaskStopped, LoginExpiredError, VerificationRequiredError):
                raise
            except Exception as exc:
                self._failures += 1
                self.database.record_content_failure(ans, self.task_id, request.as_options(), exc)
                if isinstance(exc, BrowserOperationTimeout):
                    raise
                return {"id": ans_id, "title": ans.title, "author": author_name, "status": "正文获取失败"}
        if (
            ans.zhihu_content_id != ans_id
            or ans.content_incomplete
            or answer_body_needs_enrichment(ans.html_content)
            or not _body_is_usable(ans)
        ):
            self._failures += 1
            self.database.record_content_failure(ans, self.task_id, request.as_options(), "正文仍不完整或身份不一致，未保存为完整内容")
            return {"id": ans_id, "title": ans.title, "author": author_name, "status": "正文获取失败"}
        images_before = self._failed_images
        exports_before = self._failed_exports
        try:
            self._persist_record(
                ans,
                collection_dir,
                request,
                downloader,
                pdf,
                index_scope="collection",
                preserve_existing_images=reused,
            )
        except (TaskStopped, LoginExpiredError, VerificationRequiredError):
            raise
        except RuntimeError as exc:
            return {"id": ans_id, "title": ans.title, "author": ans.author.name, "status": f"单篇导出失败: {exc}"}
        except Exception as exc:
            self._failures += 1
            self.database.record_content_failure(ans, self.task_id, request.as_options(), exc)
            if isinstance(exc, BrowserOperationTimeout):
                raise
            return {"id": ans_id, "title": ans.title, "author": author_name, "status": "正文获取失败"}
        if self._failed_images > images_before:
            status = "图片部分失败"
        elif self._failed_exports > exports_before:
            status = "单篇导出失败"
        elif request.export_separate and exports_user_files(request):
            status = "成功"
        else:
            status = "已保存"
        return {"id": ans_id, "title": ans.title, "author": ans.author.name, "status": status}

    def _rows_in_order(self, zhihu_ids: list[str]):
        rows = []
        for zhihu_id in zhihu_ids:
            found = self.database.list_contents(zhihu_content_id=str(zhihu_id), content_type="answer")
            if not found:
                return None
            rows.append(found[0])
        return rows

    def _finish_collection_run(
        self,
        manifest,
        results_log: list[dict],
        answers_to_export,
        merge_order_ids: list[str],
        collection_dir: Path,
        request: CollectRequest,
        pdf,
        snapshot_id: int,
        *,
        interrupted: bool = False,
        merge_note: str = "",
    ) -> None:
        selected = len(answers_to_export)
        unprocessed = max(0, selected - len(results_log))
        for ans in list(answers_to_export)[len(results_log):]:
            results_log.append({
                "id": ans.zhihu_content_id,
                "title": ans.title,
                "author": ans.author.name,
                "status": "未处理",
            })
        if manifest.reached_limit:
            self._hit_max_items = True
        subset = bool(request.retry_content_ids) or (
            request.confirmed_collection_member_ids is not None
            and len(request.confirmed_collection_member_ids) < len(manifest.answers)
        )
        gaps = []
        if manifest.integrity_status == "partial":
            gaps.append(manifest.finish_reason or "清单不完整")
        elif manifest.integrity_status == "unknown":
            gaps.append(manifest.finish_reason or "没有可靠的收藏夹结束证据")
        elif manifest.integrity_status == "empty":
            gaps.append("收藏夹没有回答")
        if subset and request.retry_content_ids:
            gaps.append("本次只恢复失败正文")
        elif subset:
            gaps.append("本次只导出确认范围中的一部分")
        if manifest.reached_limit:
            gaps.append(manifest.finish_reason or "达到篇数上限")
        if unprocessed:
            gaps.append(f"{unprocessed} 篇未处理")
        unknown_count = int(getattr(manifest, "unknown_type_count", 0) or 0)
        if unknown_count:
            gaps.append(f"已导出已识别回答，另有{unknown_count}条未识别")
        if interrupted:
            gaps.append(manifest.finish_reason or "任务已中断")
        if merge_note:
            gaps.append(merge_note)
        archive_ok = (
            not interrupted
            and not subset
            and not manifest.reached_limit
            and unprocessed == 0
            and manifest.integrity_status == "complete"
            and unknown_count == 0
            and self._failures == 0
            and self._failed_images == 0
            and self._failed_exports == 0
            and not merge_note
        )
        empty_ok = (
            not interrupted
            and manifest.integrity_status == "empty"
            and unknown_count == 0
            and not answers_to_export
            and self._failures == 0
            and self._failed_images == 0
            and self._failed_exports == 0
        )
        self._collection_ok = archive_ok or empty_ok
        self._collection_gap = "" if self._collection_ok else "；".join(dict.fromkeys(gaps))
        self._collection_status_text = "收藏夹为空：没有可导出的回答。" if empty_ok else ""
        final_integrity = manifest.integrity_status
        if not self._collection_ok and final_integrity == "complete":
            final_integrity = "partial"
        if manifest.integrity_status == "unknown" and not answers_to_export:
            final_integrity = "unknown"
        self.database.update_collection_snapshot(snapshot_id, finished=not interrupted, integrity_status=final_integrity)
        self._write_collection_result_report(
            manifest,
            results_log,
            collection_dir,
            request,
            selected_count=selected,
            final_integrity=final_integrity,
        )

    @serialized_export
    def _write_collection_merged(self, manifest, rows, collection_dir: Path, request: CollectRequest, pdf: PdfExporter) -> tuple[int, int]:
        destination = collection_dir / safe_filename(manifest.title, extension=".md", max_length=80)
        merged, pending = render_merged_rows(
            self.database, rows, destination, title=manifest.title, author="知乎收藏夹",
            save_metadata=request.save_metadata, anchor_field="id", time_field="updated",
            controller=self.controller,
        )
        primary_id = rows[0]["id"] if rows else 0
        scope_json = merged_scope_json(
            title=manifest.title, author="知乎收藏夹", rows=rows, request=request,
            anchor_field="id", time_field="updated", pending=pending, collection_id=manifest.collection_id,
        )

        conflict_policy = getattr(request, "conflict_policy", "new_copy")
        formats = _requested_formats(request)
        if conflict_policy == "new_copy":
            base_name = destination.stem
            needed_suffixes = [s for _, s in formats]
            if any((collection_dir / f"{base_name}{s}").exists() for s in needed_suffixes):
                copy_idx = 1
                while any((collection_dir / f"{base_name}_副本{copy_idx}{s}").exists() for s in needed_suffixes):
                    copy_idx += 1
                destination = collection_dir / f"{base_name}_副本{copy_idx}.md"

        failed_count = 0
        skipped_count = 0
        for export_format, suffix in formats:
            self.controller.checkpoint()
            target = destination if suffix == ".md" else destination.with_suffix(suffix)
            if conflict_policy == "skip" and target.exists():
                skipped_count += 1
                continue
            if recovery_keeps_existing_target(self.database, target, export_format, request.recovery_not_before, request.recovery_not_before_seq):
                self._log(f"正文恢复未覆盖后续导出：{target.name}")
                skipped_count += 1
                continue
            try:
                _write_export_file(export_format, merged, collection_dir, target, pdf, manifest.title)
                self.database.record_export(
                    primary_id,
                    export_format=export_format,
                    output_path=str(target.resolve()),
                    content_hash=sha256_text(merged),
                    status="success",
                    fingerprint=sha256_text(merged),
                    export_type="merged",
                    scope_json=scope_json,
                )
            except TaskStopped:
                raise
            except Exception as exc:
                logger.exception("收藏夹合并导出 %s 失败：%s", target, exc)
                failed_count += 1
                self._failed_exports += 1
                self.database.record_export(
                    primary_id,
                    export_format=export_format,
                    output_path=str(target.resolve()),
                    content_hash="",
                    status="failed",
                    error_message=str(exc),
                    export_type="merged",
                    scope_json=scope_json,
                )
        return failed_count, skipped_count

    def _write_collection_result_report(
        self,
        manifest,
        results: list[dict],
        collection_dir: Path,
        request: CollectRequest,
        selected_count: int | None = None,
        final_integrity: str = "",
    ) -> None:
        if not exports_user_files(request):
            return
        from zhihu_collector.utils.time_utils import now_iso
        selected = len(manifest.answers) if selected_count is None else selected_count
        buckets = {"成功": 0, "跳过": 0, "失败": 0, "未处理": 0}
        for item in results:
            status = str(item.get("status") or "")
            if status in {"成功"}:
                buckets["成功"] += 1
            elif status == "跳过":
                buckets["跳过"] += 1
            elif status == "未处理":
                buckets["未处理"] += 1
            else:
                buckets["失败"] += 1
        integrity = final_integrity or manifest.integrity_status
        report_lines = [
            f"# 收藏夹归档结果：{manifest.title}",
            "",
            f"- **收藏夹链接**：{manifest.url}",
            f"- **收藏夹 ID**：{manifest.collection_id}",
            f"- **归档时间**：{now_iso()}",
            f"- **知乎宣称条目**：{manifest.total_declared or '未知'}",
            f"- **发现回答数量**：{len(manifest.answers)}",
            f"- **过滤非回答条目**：{manifest.non_answer_count}",
            f"- **无法确定类型条目**：{getattr(manifest, 'unknown_type_count', 0)}",
            *(
                [f"- **识别限制**：已导出已识别回答，另有{int(getattr(manifest, 'unknown_type_count', 0) or 0)}条未识别"]
                if int(getattr(manifest, "unknown_type_count", 0) or 0)
                else []
            ),
            f"- **重复条目**：{manifest.duplicate_count}",
            f"- **确认选中回答**：{selected}",
            f"- **成功**：{buckets['成功']}",
            f"- **跳过**：{buckets['跳过']}",
            f"- **失败或部分失败**：{buckets['失败']}",
            f"- **未处理**：{buckets['未处理']}",
            f"- **终态合计**：{sum(buckets.values())}",
            f"- **结束原因**：{manifest.finish_reason or '已完成'}",
            f"- **完整性状态**：{integrity}",
            "",
            "## 导出回答清单",
            "",
            "| 序号 | 回答 ID | 标题 | 作者 | 处理状态 |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
        for idx, item in enumerate(results, start=1):
            report_lines.append(
                f"| {idx} | {item.get('id')} | {item.get('title')} | {item.get('author')} | {item.get('status')} |"
            )
        report_content = "\n".join(report_lines)
        report_path = _collection_report_path(collection_dir, request.conflict_policy)
        if report_path is None:
            return
        temporary = Path(str(report_path) + ".tmp")
        temporary.write_text(report_content, encoding="utf-8")
        temporary.replace(report_path)

    def _existing_count(self, parsed, mode: str) -> int:
        if mode == "collection":
            return len(self.database.get_collection_members(parsed.collection_id or ""))
        if mode == "single_answer":
            return len(self.database.list_contents(zhihu_content_id=parsed.answer_id or "", content_type="answer"))
        if parsed.kind == "question":
            return self.database.count_contents(question_zhihu_id=parsed.question_id or "", content_type="answer")
        author_url = public_user_url(parsed.user_kind, parsed.user_token)
        if mode == "pins":
            return self.database.count_contents(author_url=author_url, content_type="pin")
        if mode == "all":
            return self.database.count_contents(author_url=author_url, content_type="answer") + self.database.count_contents(
                author_url=author_url,
                content_type="pin",
            )
        return self.database.count_contents(author_url=author_url, content_type="answer")

    def _completed_count(self, *, author_url: str, content_type: str) -> int:
        # Prior library rows outside this run's discovered scope are not completed work.
        return self._new_saved + self._updated_saved + self._skipped

    def _found_count(self, content_type: str, totals: int | None, seen: int, completed: int) -> int:
        del completed
        self._seen_by_type[content_type] = seen
        if isinstance(totals, int):
            self._totals_by_type[content_type] = totals
        if getattr(self, "mode", "") == "all":
            return sum(self._seen_by_type.get(kind, 0) for kind in ("answer", "pin"))
        return seen

    def _listing_integrity_notice(self) -> str:
        notices = []
        for state in self._listing_states:
            evidence = state.end_evidence
            if self._hit_max_items:
                intentional = "limit"
            elif evidence == "incremental":
                intentional = "incremental"
            else:
                intentional = ""
            text = closure_notice(
                saved_total=int(getattr(state, "saved_total", 0) or 0),
                saved_new=self._new_saved,
                saved_updated=self._updated_saved,
                reused=self._skipped,
                scanned=len(state.seen_ids),
                declared=state.declared_total if isinstance(state.declared_total, int) else None,
                end_evidence=evidence,
                totals_seen=list(state.totals_seen or []),
                unparsed=int(state.unparsed_count or 0),
                unknown=int(state.unknown_type_count or 0),
                intentional=intentional,
            )
            if text:
                notices.append(text)
        return "".join(notices)

    def _listing_progress_totals(self) -> tuple[int | None, int | None]:
        if not self._listing_states:
            return None, None
        scanned = sum(len(state.seen_ids) for state in self._listing_states)
        declared_values = [state.declared_total for state in self._listing_states if isinstance(state.declared_total, int)]
        declared = sum(declared_values) if declared_values else None
        return scanned, declared

    def _video_note(self) -> str:
        slots = self._video_slots
        if slots <= 0:
            return ""
        text = (
            f"其中{len(self._video_answer_ids)}篇含视频，共{slots}处视频引用；"
            "已保留标题和观看入口，未下载视频文件。"
        )
        if self._video_fallbacks:
            text += f"其中{self._video_fallbacks}处未取得独立视频链接，已保留原回答入口。"
        return text

    def _record_listing_page(self, row: dict) -> None:
        row = dict(row)
        row["task_id"] = self.task_id
        try:
            append_jsonl(log_dir() / "diagnostics", self.task_id, row)
        except Exception:
            logger.exception("分页诊断写入失败")

    def _write_listing_summary(self) -> None:
        if not self._listing_states or not self.task_id:
            return
        scanned: set[str] = set()
        skipped: set[str] = set()
        for state in self._listing_states:
            scanned.update(state.seen_ids)
            skipped.update(state.skipped_ids)
        pending = sorted(scanned - self._body_saved_ids - self._body_failed_ids - skipped)
        row = {
            "kind": "summary",
            "task_id": self.task_id,
            "mode": getattr(self, "mode", ""),
            "scanned_ids": sorted(scanned),
            "saved_ids": sorted(self._body_saved_ids),
            "failed_ids": sorted(self._body_failed_ids),
            "pending_ids": pending,
            "end_evidence": [state.end_evidence for state in self._listing_states],
            "declared_totals": [state.declared_total for state in self._listing_states],
            "duplicate_count": sum(int(state.duplicate_count or 0) for state in self._listing_states),
            "unparsed_count": sum(int(state.unparsed_count or 0) for state in self._listing_states),
        }
        try:
            append_jsonl(log_dir() / "diagnostics", self.task_id, row)
        except Exception:
            logger.exception("分页汇总写入失败")

    def _report(self, status: str, *, found: int, completed: int, current: str = "", downloader=None, scanned: int | None = None, declared: int | None = None) -> None:
        self.progress.status = status
        self.progress.found = found
        self.progress.completed = completed
        self.progress.failed = self._failures
        self.progress.current_title = current or self.progress.current_title
        self.progress.message = status
        self.progress.saved_new = self._new_saved
        self.progress.saved_updated = self._updated_saved
        self.progress.reused_existing = self._skipped
        if scanned is not None:
            self.progress.scanned = scanned
        if declared is not None:
            self.progress.declared_total = declared
        self._apply_percent(found, completed, scanned)
        if downloader is not None and status == "正在搜集":
            elapsed = max(time.time() - self._started, 0.1)
            self.progress.message = f"{status}  {downloader.bytes_downloaded / 1024 / elapsed:.1f} KB/s"
        self.on_progress(self.progress)
        if scanned is not None:
            detail = f"已扫描 {scanned}，本次新增 {self._new_saved}"
            if declared:
                detail += f"，网站参考 {declared}"
        else:
            detail = f"已发现 {found}，已完成 {completed}"
        self._log(f"{status} {detail}" + (f"：{current}" if current else ""))

    def _apply_percent(self, found: int, completed: int, scanned: int | None) -> None:
        declared = self.progress.declared_total
        if self.progress.integrity == "unconfirmed":
            if declared and scanned is not None and declared > 0:
                self.progress.percent = min(99.0, scanned / declared * 100)
            elif self.progress.percent is not None:
                self.progress.percent = min(99.0, self.progress.percent)
            return
        if self.progress.integrity == "complete":
            self.progress.percent = 100.0
            return
        if declared and declared > 0 and scanned is not None:
            self.progress.percent = min(99.0, scanned / declared * 100)
            return
        if scanned:
            done = self._new_saved + self._updated_saved + self._skipped + self._failures
            self.progress.percent = min(99.0, done / scanned * 100)
            return
        if found:
            self.progress.percent = min(99.0, completed / found * 100)
            return
        self.progress.percent = None

    def _failure_message(self, exc: BaseException) -> str:
        diagnostic = failure_diagnostic(exc)
        if diagnostic is not None:
            try:
                append_jsonl(log_dir() / "connection_diagnostics", self.task_id, diagnostic)
            except OSError:
                logger.warning("连接诊断文件写入失败；保留原任务错误", exc_info=True)
        return explain_failure(exc) or str(exc) or exc.__class__.__name__

    def _log(self, message: str) -> None:
        logger.info(message)
        self.on_log(message)

@serialized_export
def _body_is_usable(record: ContentRecord) -> bool:
    html = record.html_content or ""
    if not html.strip():
        return False
    if "<img" in html.lower() or html_has_video_reference(html):
        return True
    return bool(BeautifulSoup(html, "lxml").get_text(" ", strip=True))


def classify_completion(message: str) -> str:
    text = message or ""
    if text.startswith("搜集失败："):
        return "failed"
    if text.startswith("搜集结束，但有") or "失败项" in text or "未完成项" in text:
        return "partial"
    if "无法确认全量完整" in text or "完整性未确认" in text:
        return "unconfirmed"
    return "complete"


def _can_reuse_saved_answer(existing_row, record: ContentRecord) -> bool:
    """已有完整正文只在仍然有效时复用。新的完整正文不能被旧 HTML 换掉。"""
    from zhihu_collector.parsers.collection_parser import answer_body_needs_enrichment
    from zhihu_collector.utils.time_utils import unix_to_iso

    if existing_row is None:
        return False
    html = existing_row["html_content"] or ""
    if not html.strip() or answer_body_needs_enrichment(html):
        return False
    new_updated = unix_to_iso(record.updated_time) if record.updated_time else ""
    old_updated = existing_row["updated_time"] or ""
    if new_updated and old_updated and new_updated > old_updated:
        return False
    if record.html_content and not record.content_incomplete and not answer_body_needs_enrichment(record.html_content):
        return record.content_hash == (existing_row["content_hash"] or "")
    return True


def _image_is_ready(image: dict) -> bool:
    return image_is_ready(image)


def _normalize_stored_image(image: dict) -> dict:
    row = dict(image)
    if row.get("download_status") == "downloaded":
        problem = image_problem(row)
        if problem:
            row["download_status"] = "failed"
            row["error"] = row.get("error") or problem
    return row


def _accepts_keyword(function, name: str) -> bool:
    import inspect

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _image_failure_count(images: list[dict]) -> int:
    return sum(1 for image in images if image.get("original_url") and not _image_is_ready(image))


def _merge_image_rows(existing: list[dict], new_rows: list[dict], *, keep_existing: bool) -> list[dict]:
    existing_by_url = {image.get("original_url") or "": image for image in existing if image.get("original_url")}
    if not new_rows:
        return [dict(image) for image in existing] if keep_existing else []
    merged = []
    seen: set[str] = set()
    for row in new_rows:
        url = row.get("original_url") or ""
        previous = existing_by_url.get(url)
        chosen = dict(previous) if previous is not None and _image_is_ready(previous) and not _image_is_ready(row) else dict(row)
        if url and url in seen:
            continue
        seen.add(url)
        merged.append(chosen)
    if keep_existing:
        for url, image in existing_by_url.items():
            if url not in seen:
                merged.append(dict(image))
    return merged


def recovery_keeps_existing_target(
    database,
    target: Path,
    export_format: str,
    not_before: str,
    not_before_seq: int = 0,
) -> bool:
    """正文恢复不能用旧任务的覆盖权限替换此后已经成功导出的文件。

    先后顺序优先用导出与失败共用的事件序号判断，同一秒内也能区分；
    旧记录没有序号时退回秒级时间，相同时刻无法区分则保守保留现有文件。
    """
    if database is None or not not_before or not target.exists():
        return False
    row = database.latest_successful_export_for_target(str(target), export_format)
    if row is None:
        return False
    exported_at = row["updated_at"] or ""
    export_seq = int(row["event_seq"] or 0) if "event_seq" in row.keys() else 0
    failure_seq = int(not_before_seq or 0) or database.failure_seq_at(not_before)
    if export_seq and failure_seq:
        return export_seq > failure_seq
    return exported_at >= not_before


def _collection_report_path(collection_dir: Path, conflict_policy: str) -> Path | None:
    report_path = collection_dir / "导出结果.md"
    if conflict_policy == "skip" and report_path.exists():
        return None
    if conflict_policy == "new_copy" and report_path.exists():
        copy_index = 1
        candidate = collection_dir / f"导出结果_副本{copy_index}.md"
        while candidate.exists():
            copy_index += 1
            candidate = collection_dir / f"导出结果_副本{copy_index}.md"
        return candidate
    return report_path


def pending_image_urls(images: list[dict]) -> list[str]:
    """渲染时仍不能用本地文件替换的图片地址，记入导出快照，图片恢复后据此找到受影响的产物。"""
    return list(dict.fromkeys(
        image.get("original_url") or ""
        for image in images
        if image.get("original_url") and not image_is_ready(image)
    ))


def separate_scope_json(request: CollectRequest, images: list[dict]) -> str:
    """单篇导出的渲染选项快照。失败重试和图片恢复按它还原，不套用当前全局设置。"""
    return json.dumps({
        "kind": "separate",
        "save_metadata": bool(request.save_metadata),
        "conflict_policy": getattr(request, "conflict_policy", "new_copy"),
        "pending_images": pending_image_urls(images),
    }, ensure_ascii=False)


def render_separate_markdown(row, images: list[dict], destination: Path, save_metadata: bool) -> str:
    body = html_to_markdown(row["html_content"] or "") or (row["markdown_content"] or "")
    rendered = render_markdown(
        content_type=row["content_type"],
        title=row["title"] or "未命名",
        author=row["author_name"] or "",
        question=row["title"] or "",
        url=row["url"] or "",
        created_time=row["created_time"],
        updated_time=row["updated_time"],
        vote_count=row["vote_count"],
        comment_count=row["comment_count"],
        body=body,
        include_meta=save_metadata,
    )
    return localize_images(rendered, images, destination)


def render_merged_rows(
    database,
    rows,
    destination: Path,
    *,
    title: str,
    author: str,
    save_metadata: bool,
    anchor_field: str = "export_index",
    time_field: str = "created",
    controller: TaskController | None = None,
) -> tuple[str, list[str]]:
    """合并文档的唯一渲染入口。返回 (Markdown, 仍未本地化的图片地址)。"""
    pieces = []
    pending: list[str] = []
    for row in rows:
        if controller is not None:
            controller.checkpoint()
        images = [dict(image) for image in database.list_images(row["id"])]
        pending.extend(pending_image_urls(images))
        body = html_to_markdown(row["html_content"] or "") or (row["markdown_content"] or "")
        if anchor_field == "id":
            anchor = f"item-{row['id']}"
        else:
            anchor = f"item-{row['export_index'] or row['id']}"
        if time_field == "updated":
            created_at = row["updated_time"] or row["created_time"] or ""
        else:
            created_at = row["created_time"] or ""
        pieces.append({
            "anchor": anchor,
            "title": row["title"] or "未命名",
            "author": row["author_name"] or author,
            "created_at": created_at,
            "url": row["url"] or "",
            "body": localize_images(body, images, destination),
        })
    merged = render_merged_markdown(title=title, author=author, pieces=pieces, include_meta=save_metadata)
    return merged, list(dict.fromkeys(pending))


def merged_scope_json(
    *,
    title: str,
    author: str,
    rows,
    request: CollectRequest,
    anchor_field: str,
    time_field: str,
    pending: list[str],
    **extra,
) -> str:
    return json.dumps({
        "kind": "merged",
        "title": title,
        "author": author,
        "member_ids": [int(r["id"]) for r in rows],
        "save_metadata": bool(request.save_metadata),
        "conflict_policy": getattr(request, "conflict_policy", "new_copy"),
        "anchor_field": anchor_field,
        "time_field": time_field,
        "pending_images": pending,
        **extra,
    }, ensure_ascii=False)


def write_separate_exports(
    row,
    images: list[dict],
    directory: Path,
    request: CollectRequest,
    settings: Settings,
    pdf: PdfExporter | None = None,
    database=None,
) -> dict[str, str]:
    """写出尚缺的格式。同一内容更新时复用已记录路径，不同内容冲突时另起文件名。"""
    if not request.export_separate:
        return {}
    destination = allocate_markdown_path(row, directory, settings, database)
    rendered = render_separate_markdown(row, images, destination, request.save_metadata)
    scope_json = separate_scope_json(request, images)
    content_hash = row["content_hash"] or ""
    conflict_policy = getattr(request, "conflict_policy", "new_copy")
    formats = _requested_formats(request)
    fingerprints = {fmt: _export_fingerprint(row, request, images, rendered, export_format=fmt) for fmt, _ in formats}
    if conflict_policy == "new_copy":
        needs_copy = False
        for export_format, suffix in formats:
            target = destination if suffix == ".md" else destination.with_suffix(suffix)
            if not target.exists():
                continue
            is_current = database is not None and database.export_is_current(int(row["id"]), export_format, str(target), fingerprints[export_format])
            is_kept = recovery_keeps_existing_target(database, target, export_format, getattr(request, "recovery_not_before", ""), getattr(request, "recovery_not_before_seq", 0))
            if not is_current and not is_kept:
                needs_copy = True
                break
        if needs_copy:
            base_stem = destination.stem
            root_stem = re.sub(r"_副本\d+$", "", base_stem)
            taken = database.stems_owned_by_others(directory, int(row["id"])) if database is not None else set()
            copy_idx = 1
            while True:
                candidate_stem = f"{root_stem}_副本{copy_idx}"
                if candidate_stem.lower() not in taken and not any((directory / f"{candidate_stem}{s}").exists() for s in _EXPORT_SUFFIXES):
                    break
                copy_idx += 1
            destination = directory / f"{candidate_stem}.md"
            if database is not None:
                database.remember_export_stem(int(row["id"]), directory, destination.stem)
    results: dict[str, str] = {}
    for export_format, suffix in formats:
        fingerprint = fingerprints[export_format]
        target = destination if suffix == ".md" else destination.with_suffix(suffix)
        if conflict_policy == "skip" and target.exists():
            results[export_format] = "skipped"
            continue
        if database is not None and database.export_is_current(int(row["id"]), export_format, str(target), fingerprint):
            results[export_format] = "success"
            continue
        if recovery_keeps_existing_target(database, target, export_format, getattr(request, "recovery_not_before", ""), getattr(request, "recovery_not_before_seq", 0)):
            logger.info("正文恢复未覆盖后续导出：%s", target.name)
            results[export_format] = "kept"
            continue
        try:
            _write_export_file(
                export_format, rendered, destination.parent, target, pdf, row["title"] or "知乎内容",
                frontmatter=bool(request.save_metadata),
            )
            if database is not None:
                database.record_export(
                    int(row["id"]),
                    export_format=export_format,
                    output_path=str(target),
                    content_hash=content_hash,
                    status="success",
                    fingerprint=fingerprint,
                    scope_json=scope_json,
                )
            results[export_format] = "success"
        except TaskStopped:
            raise
        except Exception as exc:
            logger.exception("导出 %s 失败：%s", target, exc)
            if database is not None:
                database.record_export(
                    int(row["id"]),
                    export_format=export_format,
                    output_path=str(target),
                    content_hash=content_hash,
                    status="failed",
                    error_message=str(exc),
                    fingerprint=fingerprint,
                    scope_json=scope_json,
                )
            results[export_format] = str(exc)
    return results


def export_saved_rows(
    database,
    rows,
    root: Path,
    request: CollectRequest,
    settings: Settings,
    pdf=None,
    downloader=None,
    controller: TaskController | None = None,
) -> int:
    """从数据库补做缺失或失败的导出，不重新抓取未变化正文。返回失败篇数。"""
    failures = 0
    for row in rows:
        if controller is not None:
            controller.checkpoint()
        images = [dict(image) for image in database.list_images(row["id"])]
        if downloader is not None:
            images = retry_failed_images(database, row, root / "images", downloader)
        results = write_separate_exports(row, images, root / ("想法" if row["content_type"] == "pin" else "回答"), request, settings, pdf, database)
        if any(status not in {"success", "skipped", "kept"} for status in results.values()):
            failures += 1
    return failures


def retry_failed_images(database, row, image_dir: Path, downloader) -> list[dict]:
    """重试不可用的图片：未下载、本地文件缺失、为空或与登记摘要不一致的都要重下。"""
    current = [dict(image) for image in database.list_images(row["id"])]
    failed_urls = []
    for image in current:
        if image_problem(image) and image.get("original_url"):
            failed_urls.append(image["original_url"])
    if not failed_urls:
        return current
    downloaded = {
        item.original_url: item.as_row()
        for item in downloader.download_all(
            failed_urls,
            image_dir,
            f"{row['content_type']}_{row['zhihu_content_id'] or row['id']}",
            indexes=[int(image.get("sort_index") or 1) for image in current if (image.get("original_url") or "") in set(failed_urls)],
            # 带上登记摘要，损坏的旧文件不能因为文件名和标记仍在就被直接复用。
            known_hashes={
                image["original_url"]: image.get("hash") or ""
                for image in current
                if image.get("original_url") in set(failed_urls) and image.get("hash")
            },
        )
    }
    merged = []
    for image in current:
        updated = downloaded.get(image.get("original_url"))
        if updated is None:
            merged.append(image)
            continue
        updated["sort_index"] = image.get("sort_index", updated.get("sort_index", 0))
        merged.append(updated)
    database.replace_images(int(row["id"]), merged)
    return merged


def allocate_markdown_path(row, directory: Path, settings: Settings, database=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    content_local_id = int(row["id"])
    if database is not None:
        remembered = database.export_stem(content_local_id, directory)
        if remembered:
            return directory / f"{remembered}.md"
    filename = apply_filename_rule(
        settings.filename_rule,
        index=int(row["export_index"] or row["id"]),
        title=row["title"] or row["zhihu_content_id"],
        content_id=row["zhihu_content_id"],
        extension=".md",
    )
    candidate = directory / filename
    taken = database.stems_owned_by_others(directory, content_local_id) if database is not None else set()
    chosen = _collision_free(candidate, row["zhihu_content_id"] or str(content_local_id), taken)
    if database is not None:
        database.remember_export_stem(content_local_id, directory, chosen.stem)
    return chosen


_EXPORT_SUFFIXES = (".md", ".docx", ".pdf")


def _collision_free(candidate: Path, content_key: str, taken_stems: set[str]) -> Path:
    directory = candidate.parent

    def occupied(path: Path) -> bool:
        if path.stem.lower() in taken_stems:
            return True
        return any((directory / f"{path.stem}{suffix}").exists() for suffix in _EXPORT_SUFFIXES)

    if not occupied(candidate):
        return candidate
    extra = safe_filename(str(content_key), fallback="item", max_length=24)
    alternate = candidate.with_name(f"{candidate.stem}_{extra}{candidate.suffix}")
    if not occupied(alternate):
        return alternate
    number = 2
    probe = alternate
    while occupied(probe):
        probe = alternate.with_name(f"{alternate.stem}_{number}{alternate.suffix}")
        number += 1
    return probe


def _export_fingerprint(row, request: CollectRequest, images: list[dict], rendered: str, *, export_format: str = "") -> str:
    payload = {
        "rendered": rendered,
        "save_metadata": bool(request.save_metadata),
        "filename_rule": "",
        "images": [
            {
                "url": image.get("original_url") or "",
                "status": image.get("download_status") or "",
                "path": image.get("local_path") or "",
                "hash": image.get("hash") or "",
            }
            for image in images
        ],
        "content_hash": row["content_hash"] or "",
    }
    if export_format == "docx":
        # 旧版 Word 转义解析有缺陷，升级后不能把旧产物直接当成已完成。
        payload["docx_renderer"] = "markdown-inline-v2"
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _requested_formats(request: CollectRequest) -> list[tuple[str, str]]:
    selected = []
    if request.export_markdown:
        selected.append(("markdown", ".md"))
    if request.export_docx:
        selected.append(("docx", ".docx"))
    if request.export_pdf:
        selected.append(("pdf", ".pdf"))
    return selected


def _write_export_file(
    export_format: str,
    rendered: str,
    base_dir: Path,
    target: Path,
    pdf,
    title: str,
    frontmatter: bool | None = None,
) -> None:
    """原子写入：临时文件写完并通过产物校验后才替换目标；失败时保留原文件。

    frontmatter 说明 rendered 是否带程序生成的元数据头（由元数据开关决定），
    避免把以分隔线开头的正文当成元数据丢掉。
    """
    temporary = Path(str(target) + ".tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        if export_format == "markdown":
            temporary.write_text(rendered, encoding="utf-8")
        elif export_format == "docx":
            markdown_to_docx(rendered, temporary, base_dir=base_dir, frontmatter=frontmatter)
            _insert_toc(temporary)
        elif export_format == "pdf":
            if pdf is None:
                raise RuntimeError("未准备 PDF 导出")
            if frontmatter is not None and _accepts_keyword(pdf.write, "frontmatter"):
                pdf.write(rendered, temporary, title=title, frontmatter=frontmatter)
            else:
                pdf.write(rendered, temporary, title=title)
        else:
            raise RuntimeError(f"未知导出格式：{export_format}")
        problem = artifact_problem(temporary, export_format)
        if problem:
            raise RuntimeError(f"生成的文件无效（{problem}）：{target.name}")
        temporary.replace(target)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _author_folder(output_root: Path, name: str, token: str) -> Path:
    label = safe_filename(name or token or "知乎用户", max_length=40)
    if token and token not in label:
        label = safe_filename(f"{label}-{token}", max_length=70)
    return output_root / label


def _resolve_mode(mode: str, parsed) -> str:
    if mode == "auto":
        if parsed.kind == "collection":
            return "collection"
        if parsed.kind == "user":
            return "all"
        if parsed.kind == "answer":
            return "single_answer"
        if parsed.kind == "question":
            return "question"
        raise UnsupportedUrlError(parsed.raw)
    if parsed.kind == "collection":
        return "collection"
    if mode == "collection":
        if parsed.kind != "collection":
            raise CollectorError("收藏夹搜集需要知乎收藏夹链接（格式：https://www.zhihu.com/collection/xxx）。")
        return "collection"
    if mode == "single_answer":
        if parsed.kind != "answer":
            raise CollectorError("单篇回答搜集需要知乎回答链接（格式：.../question/xxx/answer/yyy 或 .../answer/yyy）。")
        return "single_answer"
    if mode == "question":
        if not parsed.question_id:
            raise CollectorError("问题搜集需要知乎问题链接或包含问题ID的回答链接。")
        return "question"
    if mode in {"answers", "pins", "all"}:
        if parsed.kind != "user":
            raise CollectorError("答主搜集需要知乎用户主页链接。")
        return mode
    raise CollectorError(f"不支持的搜集类型：{mode}")


def _insert_toc(docx_path: Path) -> None:
    try:
        from docx import Document

        document = Document(docx_path)
        # 合并文档在 markdown 转换时已经有“目录”标题。这里补一个 Word 域，方便更新页码。
        if document.paragraphs:
            add_toc_field(document)
            document.save(docx_path)
    except Exception as exc:
        logger.warning("写入 Word 目录域失败：%s", exc)
