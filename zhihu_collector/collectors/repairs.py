"""只重试失败的导出和图片，不重新采集正文。"""

from __future__ import annotations

import json
from pathlib import Path
from zhihu_collector.database.database import serialized_export

from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.offline import OfflineResult
from zhihu_collector.collectors.service import (
    CollectRequest,
    _export_fingerprint,
    _write_export_file,
    pending_image_urls,
    render_merged_rows,
    render_separate_markdown,
    retry_failed_images,
    write_separate_exports,
)
from zhihu_collector.config import Settings
from zhihu_collector.downloader.image_downloader import ImageDownloader
from zhihu_collector.errors import TaskStopped
from zhihu_collector.exporters.pdf_exporter import PdfExporter
from zhihu_collector.utils.artifacts import image_is_ready
from zhihu_collector.utils.hashing import sha256_text
from zhihu_collector.utils.logger import get_logger

logger = get_logger("repairs")


def retry_body_failures(database, settings, controller=None, on_progress=None):
    """Explicitly networked recovery; never called by offline export/image retry."""
    from dataclasses import fields
    from zhihu_collector.collectors.service import CollectionService
    from zhihu_collector.errors import LoginExpiredError, VerificationRequiredError
    from zhihu_collector.utils.urls import parse_zhihu_url

    controller = controller or TaskController()
    items = [dict(x) for x in database.list_failures() if x['kind'] == 'body']
    completed = failed = 0
    for index, item in enumerate(items):
        try:
            controller.checkpoint()
            if on_progress:
                on_progress(index, len(items), item['title'] or item['detail'])
            options = json.loads(item['options_json'])
            options = {key: value for key, value in options.items() if key in {f.name for f in fields(CollectRequest)}}
            parsed = parse_zhihu_url(options['url'])
            if parsed.kind == 'user':
                options['mode'] = 'pins' if item['content_type'] == 'pin' else 'answers'
                options.update(task_id=None, update_only=False, max_items=1,
                               retry_content_ids=[item['zhihu_content_id']])
            elif parsed.kind == 'question':
                options['mode'] = 'question'
                options.update(task_id=None, update_only=False, max_items=1,
                               retry_content_ids=[item['zhihu_content_id']])
            elif parsed.kind == 'collection':
                options['mode'] = 'collection'
                # 只恢复失败回答，并沿用原确认快照；不再进入人工确认。
                options['collection_scope_confirmation'] = False
                options.update(task_id=None, update_only=False, max_items=0,
                               retry_content_ids=[item['zhihu_content_id']])
            else:
                options.update(url=item['detail'], mode='single_answer', task_id=None,
                               update_only=False, max_items=1,
                               retry_content_ids=[item['zhihu_content_id']])
            service = CollectionService(settings, database, controller)
            service.run(CollectRequest(**options))
            remaining = {x['item_id'] for x in database.list_failures() if x['kind'] == 'body'}
            if item['item_id'] in remaining:
                failed += 1
            else:
                completed += 1
        except TaskStopped:
            return OfflineResult('cancelled', completed, failed, '已取消正文恢复，未完成项仍保留。')
        except (LoginExpiredError, VerificationRequiredError) as exc:
            return OfflineResult('needs_login', completed, failed + 1, str(exc))
        except Exception:
            logger.exception('恢复正文失败：%s', item['detail'])
            failed += 1
        if on_progress:
            on_progress(index + 1, len(items), item['title'] or item['detail'])
    other_failures = [x for x in database.list_failures() if x['kind'] != 'body']
    status = 'partial' if failed or other_failures else 'success'
    return OfflineResult(status, completed, failed,
        f'正文恢复：成功 {completed} 篇，未完成 {failed} 篇。'
        + ('图片或导出仍有失败，请在失败项中处理。' if other_failures else ''))


def _is_merged_target(target_path: str, content_type: str = "", export_type: str = "", scope_json: str = "") -> bool:
    if export_type == 'separate':
        return False
    # Unknown historical records must be handled by the guarded scope path.
    return True


def _export_scope(item) -> dict:
    try:
        scope = json.loads(item["scope_json"] or "{}")
    except (KeyError, IndexError, TypeError, ValueError):
        return {}
    return scope if isinstance(scope, dict) else {}


@serialized_export
def _retry_merged_export(
    database,
    item,
    settings: Settings,
    pdf: PdfExporter | None,
    save_metadata: bool,
    controller: TaskController | None = None,
) -> bool:
    if not database.export_retry_is_current(item, status=item.get("retry_status", "failed") if isinstance(item, dict) else "failed"):
        return True  # A newer result superseded this snapshot; never write it again.
    target_path = Path(item["target"])
    export_format = item["detail"]
    content_id = int(item["content_id"])

    scope_json = item["scope_json"] if "scope_json" in item.keys() else ""
    scope_data = _export_scope(item)

    rows = []
    title = scope_data.get("title")
    author = scope_data.get("author")
    if "save_metadata" in scope_data:
        save_metadata = scope_data["save_metadata"]

    if scope_data.get("member_ids"):
        member_ids = scope_data["member_ids"]
        for cid in member_ids:
            r = database.get_content(cid)
            if r is not None:
                rows.append(r)
        if len(rows) != len(member_ids):
            logger.warning("合并导出重试失败：原成员记录已不存在 %s", target_path)
            database.record_export(
                content_id,
                export_format=export_format,
                output_path=str(target_path.resolve()),
                content_hash="",
                status="failed",
                error_message="原成员记录已在库中不存在",
                export_type="merged",
                scope_json=scope_json,
            )
            return False
        if not title:
            title = target_path.stem
        if not author:
            author = rows[0]["author_name"] if rows else "知乎作者"
    else:
        message = '历史失败记录缺少所选范围或明确类型，无法自动恢复。请重新选择内容后导出。'
        database.record_export(content_id, export_format=export_format, output_path=str(target_path),
            content_hash='', status='failed', error_message=message, export_type='legacy', scope_json='')
        return False

    merged, pending = render_merged_rows(
        database, rows, target_path, title=title, author=author, save_metadata=save_metadata,
        anchor_field=scope_data.get("anchor_field", "id" if scope_data.get("collection_id") or title == "所选内容" else "export_index"),
        time_field=scope_data.get("time_field", "updated" if scope_data.get("collection_id") or title == "所选内容" else "created"),
        controller=controller,
    )
    updated_scope = json.dumps({**scope_data, "pending_images": pending}, ensure_ascii=False)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_export_file(export_format, merged, target_path.parent, target_path, pdf, title, frontmatter=bool(save_metadata))
        if not target_path.exists() or target_path.stat().st_size == 0:
            raise RuntimeError(f"文件写入异常，生成文件为空: {target_path}")
        database.record_export(
            content_id,
            export_format=export_format,
            output_path=str(target_path.resolve()),
            content_hash=sha256_text(merged),
            status="success",
            fingerprint=sha256_text(merged),
            export_type="merged",
            scope_json=updated_scope,
        )
        return True
    except TaskStopped:
        raise
    except Exception as exc:
        logger.exception("重试合并导出 %s 失败：%s", target_path, exc)
        database.record_export(
            content_id,
            export_format=export_format,
            output_path=str(target_path.resolve()),
            content_hash="",
            status="failed",
            error_message=str(exc),
            export_type="merged",
            scope_json=scope_json,
        )
        return False


@serialized_export
def _retry_separate_export(
    database,
    item,
    images: list[dict],
    settings: Settings,
    pdf: PdfExporter | None,
    save_metadata: bool,
) -> bool:
    if not database.export_retry_is_current(item, status=item.get("retry_status", "failed") if isinstance(item, dict) else "failed"):
        return True  # A newer result superseded this snapshot; never write it again.
    target_path = Path(item["target"])
    export_format = item["detail"]
    content_id = int(item["content_id"])
    row = database.get_content(content_id)
    if row is None:
        return False
    scope = _export_scope(item)
    if isinstance(scope.get("save_metadata"), bool):
        save_metadata = scope["save_metadata"]
    elif not scope:
        logger.warning("历史单篇失败记录没有保存元数据选项，使用本次重试选项：%s", target_path)
    rendered = render_separate_markdown(row, images, target_path, save_metadata)
    content_hash = row["content_hash"] or ""
    req = CollectRequest(
        url="offline",
        export_markdown=export_format == "markdown",
        export_docx=export_format == "docx",
        export_pdf=export_format == "pdf",
        save_metadata=save_metadata,
    )
    fingerprint = _export_fingerprint(row, req, images, rendered, export_format=export_format)
    scope.update(kind="separate", save_metadata=save_metadata)
    updated_scope = json.dumps({**scope, "pending_images": pending_image_urls(images)}, ensure_ascii=False)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_export_file(export_format, rendered, target_path.parent, target_path, pdf, row["title"] or "知乎内容", frontmatter=bool(save_metadata))
        if not target_path.exists() or target_path.stat().st_size == 0:
            raise RuntimeError(f"文件写入异常，生成文件为空: {target_path}")
        database.record_export(
            content_id,
            export_format=export_format,
            output_path=str(target_path.resolve()),
            content_hash=content_hash,
            status="success",
            fingerprint=fingerprint,
            export_type="separate",
            scope_json=updated_scope,
        )
        return True
    except TaskStopped:
        raise
    except Exception as exc:
        logger.exception("重试单篇导出 %s 失败：%s", target_path, exc)
        database.record_export(
            content_id,
            export_format=export_format,
            output_path=str(target_path.resolve()),
            content_hash="",
            status="failed",
            error_message=str(exc),
            export_type="separate",
            scope_json=json.dumps(scope, ensure_ascii=False),
        )
        return False


@serialized_export
def _refresh_registered_export(database, registered, settings, pdf, save_metadata, controller):
    """只更新同一版本、未被用户编辑且登记了待恢复图片的产物。"""
    item = dict(registered)
    item.update(item_id=item["id"], target=item["output_path"], detail=item["format"], retry_status="success")
    target = Path(item["target"])
    if not database.export_retry_is_current(item, status="success"):
        return None
    if (_export_scope(item).get("conflict_policy") == "skip"
            or not item["file_sha256"]
            or database.recorded_artifact_problem(item, target, item["format"])):
        logger.info("保留已有文件，不自动重写：%s", target)
        return None
    controller.checkpoint()
    if item["export_type"] == "merged":
        return _retry_merged_export(database, item, settings, pdf, save_metadata, controller)
    images = [dict(img) for img in database.list_images(item["content_id"])]
    return _retry_separate_export(database, item, images, settings, pdf, save_metadata)


def retry_saved_failures(
    database,
    settings: Settings,
    output_dir: Path,
    *,
    save_metadata: bool = True,
    controller: TaskController | None = None,
    on_progress=None,
    downloader: ImageDownloader | None = None,
) -> OfflineResult:
    controller = controller or TaskController()
    failures = database.list_failures()
    if not failures:
        return OfflineResult("success", 0, 0, "没有需要重试的失败项。")
    output_dir.mkdir(parents=True, exist_ok=True)
    owns_downloader = downloader is None
    if downloader is None:
        downloader = ImageDownloader(
            concurrency=settings.image_concurrency,
            attempts=settings.retry_count,
            base_delay_ms=settings.retry_base_delay_ms,
        )
    pdf = None
    completed = 0
    failed = sum(1 for item in failures if item["kind"] == "body")
    needs_login = False
    cancelled = False
    restored_by_content: dict[int, set[str]] = {}
    preserve_count = 0

    export_failures = [item for item in failures if item["kind"] == "export"]
    image_failures = [item for item in failures if item["kind"] == "image"]

    merged_failures = [
        item for item in export_failures
        if _is_merged_target(
            item["target"],
            item["content_type"] if "content_type" in item.keys() else "",
            item["export_type"] if "export_type" in item.keys() else "",
            item["scope_json"] if "scope_json" in item.keys() else "",
        )
    ]
    separate_export_failures = [
        item for item in export_failures
        if not _is_merged_target(
            item["target"],
            item["content_type"] if "content_type" in item.keys() else "",
            item["export_type"] if "export_type" in item.keys() else "",
            item["scope_json"] if "scope_json" in item.keys() else "",
        )
    ]

    failed_urls: dict[int, set[str]] = {}
    for item in image_failures:
        failed_urls.setdefault(int(item["content_id"]), set()).add(item["detail"])
    content_ids = list(dict.fromkeys(int(item["content_id"]) for item in separate_export_failures + image_failures))
    total = len(content_ids) + len(merged_failures)

    def make_pdf():
        from zhihu_collector.utils.task_diagnostics import pdf_stage_label
        def stage_changed(stage,done,count):
            if on_progress:
                on_progress(max(0,current_step-1),total,'PDF：'+pdf_stage_label(stage,done,count))
        return PdfExporter(controller=controller,on_stage=stage_changed)

    try:
        current_step = 0
        for content_id in content_ids:
            current_step += 1
            try:
                controller.checkpoint()
            except TaskStopped:
                cancelled = True
                break
            row = database.get_content(content_id)
            title = (row["title"] if row else "") or str(content_id)
            if on_progress:
                on_progress(current_step - 1, total, title)
            if row is None:
                failed += 1
                continue
            images = retry_failed_images(database, row, _image_dir(database, row, output_dir), downloader)
            if any("登录" in (image.get("error") or "") for image in images):
                needs_login = True
                failed += 1
                break
            restored = failed_urls.get(content_id, set()) & {
                image["original_url"] for image in images if image_is_ready(image)
            }
            if restored:
                restored_by_content[content_id] = restored

            failed_items_for_content = [
                item for item in separate_export_failures
                if int(item["content_id"]) == content_id
            ]
            item_failed = False
            if failed_items_for_content:
                for failed_item in failed_items_for_content:
                    if failed_item["target"]:
                        if failed_item["detail"] == "pdf" and pdf is None:
                            pdf = make_pdf()
                        controller.checkpoint()
                        try:
                            ok = _retry_separate_export(database, failed_item, images, settings, pdf, save_metadata)
                        except TaskStopped:
                            raise
                        except Exception:
                            logger.exception('重试单篇导出失败：%s', failed_item['target'])
                            ok = False
                        if not ok:
                            item_failed = True
                    else:
                        request = _request_for(database, content_id, save_metadata)
                        if request.export_pdf and pdf is None:
                            pdf = make_pdf()
                        results = write_separate_exports(
                            row, images, _kind_dir(database, row, output_dir), request, settings, pdf, database,
                        )
                        if not results or any(status not in {"success", "skipped", "kept"} for status in results.values()):
                            item_failed = True

            has_image_failure = any(not image_is_ready(image) for image in images)
            if has_image_failure or item_failed:
                failed += 1
            else:
                completed += 1
            if on_progress:
                on_progress(current_step, total, title)

        if not cancelled and not needs_login and restored_by_content:
            for registered in database.successful_exports():
                scope = _export_scope(registered)
                pending = scope.get("pending_images")
                if not isinstance(pending, list) or not pending:
                    continue
                members = scope.get("member_ids") if registered["export_type"] == "merged" else [registered["content_id"]]
                if not isinstance(members, list):
                    continue
                if not any(restored_by_content.get(member, set()).intersection(pending) for member in members if isinstance(member, int)):
                    continue
                if registered["format"] == "pdf" and pdf is None:
                    pdf = make_pdf()
                try:
                    ok = _refresh_registered_export(database, registered, settings, pdf, save_metadata, controller)
                except TaskStopped:
                    raise
                except Exception:
                    logger.exception("更新图片关联导出失败：%s", registered["output_path"])
                    ok = False
                if ok is None:
                    preserve_count += 1
                elif not ok:
                    failed += 1

        if not cancelled and not needs_login:
            for merged_item in merged_failures:
                current_step += 1
                try:
                    controller.checkpoint()
                except TaskStopped:
                    cancelled = True
                    break
                target_title = Path(merged_item["target"]).name
                if on_progress:
                    on_progress(current_step - 1, total, target_title)
                if merged_item["detail"] == "pdf" and pdf is None:
                    pdf = make_pdf()
                try:
                    ok = _retry_merged_export(database, merged_item, settings, pdf, save_metadata, controller)
                except TaskStopped:
                    raise
                except Exception as exc:
                    logger.exception("重试合并导出 %s 异常: %s", merged_item["target"], exc)
                    ok = False
                if ok:
                    completed += 1
                else:
                    failed += 1
                if on_progress:
                    on_progress(current_step, total, target_title)

    except TaskStopped:
        cancelled = True
    finally:
        if owns_downloader:
            downloader.close()
        if pdf is not None:
            pdf.close()

    if cancelled:
        return OfflineResult("cancelled", completed, failed, f"已取消。已处理 {completed} 篇，未写完的文件没有替换旧文件。")
    if needs_login:
        return OfflineResult("needs_login", completed, failed, "图片下载时知乎要求登录。已完成的文件已保留，请重新登录后再重试。")
    if failed and completed:
        body_pending = sum(1 for item in database.list_failures() if item["kind"] == "body")
        note = f"其中 {body_pending} 篇正文待联网获取，请使用“重新获取失败正文（联网）”。" if body_pending else "剩余图片或格式可继续重试。"
        return OfflineResult("partial", completed, failed, f"部分失败：成功 {completed} 篇，未完成 {failed} 篇。" + note)
    if failed:
        return OfflineResult("partial", completed, failed, f"重试失败：共 {failed} 篇存在失败项（图片、格式或正文未完成；正文请使用“重新获取失败正文（联网）”）。")
    return OfflineResult("success", completed, failed, f"全部成功，共 {completed} 篇。"
                         + (f"有 {preserve_count} 个已有文件按保护规则保留，请按需重新导出。" if preserve_count else ""))


def _request_for(database, content_id: int, save_metadata: bool) -> CollectRequest:
    formats = database.export_formats(content_id)
    if not formats:
        formats = {"markdown"}
    return CollectRequest(
        url="offline",
        export_markdown="markdown" in formats,
        export_docx="docx" in formats,
        export_pdf="pdf" in formats,
        export_separate=True,
        export_merged=False,
        save_metadata=save_metadata,
        conflict_policy="overwrite",
    )


def _kind_dir(database, row, output_dir: Path) -> Path:
    recorded = database.latest_export_path(int(row["id"]))
    if recorded:
        return Path(recorded).parent
    return output_dir / ("想法" if row["content_type"] == "pin" else "回答")


def _image_dir(database, row, output_dir: Path) -> Path:
    kind = _kind_dir(database, row, output_dir)
    if kind.name in {"回答", "想法"}:
        return kind.parent / "images"
    return kind / "images"
