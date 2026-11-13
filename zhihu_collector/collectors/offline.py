"""用本地数据库重新导出，不访问知乎。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from zhihu_collector.database.database import serialized_export

from zhihu_collector.collectors.base import TaskController
from zhihu_collector.collectors.service import (
    CollectRequest,
    _author_folder,
    _requested_formats,
    recovery_keeps_existing_target,
    render_merged_rows,
    merged_scope_json,
    write_separate_exports,
)
from zhihu_collector.config import Settings
from zhihu_collector.errors import TaskStopped
from zhihu_collector.export_plan import expected_export_file_count
from zhihu_collector.exporters.pdf_exporter import PdfExporter
from zhihu_collector.utils.file_utils import apply_filename_rule, safe_filename


@dataclass
class OfflineResult:
    status: str
    completed: int
    failed: int
    message: str
    skipped: int = 0  # 跳过的目标文件数；completed/failed 使用内容篇数。


def _content_id(value) -> int:
    if hasattr(value, "__getitem__") and not isinstance(value, (int, str)):
        return int(value["id"])
    return int(value)


def export_local_contents(
    database,
    content_ids: list[int],
    output_dir: Path,
    request: CollectRequest,
    settings: Settings,
    controller: TaskController | None = None,
    on_progress=None,
) -> OfflineResult:
    """从已保存的正文导出。只有勾选 PDF 时才会启动用于排版的浏览器。"""
    if not _requested_formats(request) or not (request.export_separate or request.export_merged):
        return OfflineResult("failed", 0, len(content_ids), "请选择至少一种导出格式和输出方式。")
    controller = controller or TaskController()
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf = PdfExporter(controller=controller) if request.export_pdf else None
    if pdf is not None and on_progress:
        from zhihu_collector.utils.task_diagnostics import pdf_stage_label
        pdf.on_stage = lambda stage,done,total: on_progress(
            completed,len(content_ids),'PDF：'+pdf_stage_label(stage,done,total))
    completed = 0
    failed = 0
    skipped = 0
    cancelled = False
    normalized = [_content_id(item) for item in content_ids]
    rows_by_id = {}
    missing_ids = []
    for content_id in normalized:
        row = database.get_content(content_id)
        if row is None:
            missing_ids.append(content_id)
        else:
            rows_by_id[content_id] = row
    merge_blocked = bool(request.export_merged and missing_ids)
    if merge_blocked:
        failed += len(missing_ids)
        anchor = next(iter(rows_by_id.values()), None)
        if anchor is not None:
            database.record_export(
                int(anchor["id"]),
                export_format="markdown",
                output_path=str((output_dir / "所选内容.md").resolve()),
                content_hash="",
                status="failed",
                error_message="确认清单中的部分内容已不存在，未改写合并文件",
                export_type="merged",
                scope_json=json.dumps({
                    "title": "所选内容",
                    "member_ids": normalized,
                    "missing_ids": missing_ids,
                    "save_metadata": request.save_metadata,
                }, ensure_ascii=False),
            )
        if not request.export_separate:
            if pdf is not None:
                pdf.close()
            return _result(False, 0, failed, 0, output_dir, blocked_merge=True)
    rows_for_merge = []
    total = len(normalized)
    try:
        for index, content_id in enumerate(normalized, start=1):
            try:
                controller.checkpoint()
            except TaskStopped:
                cancelled = True
                break
            row = rows_by_id.get(content_id)
            title = (row["title"] if row else "") or str(content_id)
            if on_progress:
                on_progress(index - 1, total, title)
            if row is None:
                if not merge_blocked:
                    failed += 1
                continue
            images = [dict(image) for image in database.list_images(row["id"])]
            if request.export_separate:
                root = _author_folder(output_dir, row["author_name"] or "", _token(row["user_url"] or ""))
                results = write_separate_exports(
                    row,
                    images,
                    root / ("想法" if row["content_type"] == "pin" else "回答"),
                    request,
                    settings,
                    pdf,
                    database,
                )
                values = list(results.values()) if results else []
                skipped += sum(status == "skipped" for status in values)
                if values and any(status not in {"success", "skipped"} for status in values):
                    failed += 1
                elif not values or not all(status == "skipped" for status in values):
                    completed += 1
            rows_for_merge.append(row)
            if on_progress:
                on_progress(index, total, title)
        if request.export_merged and rows_for_merge and not cancelled and not merge_blocked:
            merge_failed, merge_skipped = _write_merged(database, rows_for_merge, output_dir, request, pdf, controller=controller)
            failed += merge_failed
            skipped += merge_skipped
            if not request.export_separate and merge_failed == 0 and merge_skipped == 0:
                completed += len(rows_for_merge)
    except TaskStopped:
        cancelled = True
    finally:
        if pdf is not None:
            pdf.close()
    return _result(cancelled, completed, failed, skipped, output_dir, blocked_merge=merge_blocked)


@serialized_export
def _write_merged(database, rows, output_dir: Path, request: CollectRequest, pdf, controller: TaskController | None = None) -> tuple[int, int]:
    destination = output_dir / safe_filename("所选内容", extension=".md", max_length=80)
    merged, pending = render_merged_rows(
        database, rows, destination, title="所选内容", author="本地内容库",
        save_metadata=request.save_metadata, anchor_field="id", time_field="updated", controller=controller,
    )
    from zhihu_collector.collectors.service import _write_export_file
    from zhihu_collector.utils.hashing import sha256_text

    primary_id = rows[0]["id"] if rows else 0
    failed_count = skipped_count = 0
    scope_json = merged_scope_json(
        title="所选内容", author="本地内容库", rows=rows, request=request,
        anchor_field="id", time_field="updated", pending=pending,
    )

    conflict_policy = getattr(request, "conflict_policy", "new_copy")
    formats = _requested_formats(request)
    if conflict_policy == "new_copy":
        base_name = destination.stem
        needed_suffixes = [s for _, s in formats]
        if any((output_dir / f"{base_name}{s}").exists() for s in needed_suffixes):
            copy_idx = 1
            while any((output_dir / f"{base_name}_副本{copy_idx}{s}").exists() for s in needed_suffixes):
                copy_idx += 1
            destination = output_dir / f"{base_name}_副本{copy_idx}.md"

    for export_format, suffix in formats:
        if controller is not None:
            controller.checkpoint()
        target = destination if suffix == ".md" else destination.with_suffix(suffix)
        if conflict_policy == "skip" and target.exists():
            skipped_count += 1
            continue
        if recovery_keeps_existing_target(database, target, export_format, getattr(request, "recovery_not_before", ""), getattr(request, "recovery_not_before_seq", 0)):
            skipped_count += 1
            continue
        try:
            _write_export_file(export_format, merged, output_dir, target, pdf, "所选内容")
            database.record_export(
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
            failed_count += 1
            database.record_export(
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


def _token(url: str) -> str:
    cleaned = (url or "").rstrip("/")
    if not cleaned:
        return ""
    return cleaned.split("/")[-1]


def _result(
    cancelled: bool,
    completed: int,
    failed: int,
    skipped: int,
    output_dir: Path,
    *,
    blocked_merge: bool = False,
) -> OfflineResult:
    if cancelled:
        return OfflineResult("cancelled", completed, failed, f"已取消。已完成 {completed} 篇，跳过 {skipped} 个已有文件，未完成的文件没有覆盖旧文件。\n{output_dir}", skipped)
    if blocked_merge and not completed:
        return OfflineResult("partial", 0, failed, f"确认范围中有内容已不在内容库，已保留原合并文件。失败 {failed} 篇。\n{output_dir}", skipped)
    if failed and completed:
        return OfflineResult("partial", completed, failed, f"部分失败：成功 {completed} 篇，跳过 {skipped} 个已有文件，失败 {failed} 篇。\n{output_dir}", skipped)
    if failed:
        return OfflineResult("partial", completed, failed, f"导出失败 {failed} 篇，跳过 {skipped} 个已有文件。\n{output_dir}", skipped)
    if completed == 0 and skipped:
        return OfflineResult("skipped", 0, 0, f"已按策略跳过 {skipped} 个已有文件，没有新导出。\n{output_dir}", skipped)
    if skipped:
        return OfflineResult("success", completed, failed, f"新导出 {completed} 篇，跳过 {skipped} 个已有文件。\n{output_dir}", skipped)
    return OfflineResult("success", completed, failed, f"全部成功，共 {completed} 篇。\n{output_dir}", skipped)


def offline_plan_counts(database, content_ids: list[int], output_dir: Path, settings: Settings) -> tuple[int, int]:
    """预计文件数和本次输出目录里已经存在的目标文件数。"""
    request = CollectRequest(
        url="offline",
        export_markdown=settings.export_markdown,
        export_docx=settings.export_docx,
        export_pdf=settings.export_pdf,
        export_separate=settings.export_separate,
        export_merged=settings.export_merged,
        output_dir=str(output_dir),
    )
    expected = expected_export_file_count(
        len(content_ids),
        export_markdown=request.export_markdown,
        export_docx=request.export_docx,
        export_pdf=request.export_pdf,
        export_separate=request.export_separate,
        export_merged=request.export_merged,
    )
    return expected, count_existing_export_targets(database, content_ids, output_dir, request, settings)


def count_existing_export_targets(database, content_ids: list[int], output_dir: Path, request: CollectRequest, settings: Settings) -> int:
    count = 0
    formats = _requested_formats(request)
    if request.export_merged:
        base = output_dir / safe_filename("所选内容", extension=".md", max_length=80)
        for _export_format, suffix in formats:
            target = base if suffix == ".md" else base.with_suffix(suffix)
            if target.is_file():
                count += 1
    if request.export_separate:
        for content_id in content_ids:
            row = database.get_content(int(content_id))
            if row is None:
                continue
            root = _author_folder(output_dir, row["author_name"] or "", _token(row["user_url"] or ""))
            directory = root / ("想法" if row["content_type"] == "pin" else "回答")
            remembered = database.export_stem(int(row["id"]), directory)
            if remembered:
                stem = remembered
            else:
                filename = apply_filename_rule(
                    settings.filename_rule,
                    index=int(row["export_index"] or row["id"]),
                    title=row["title"] or row["zhihu_content_id"],
                    content_id=row["zhihu_content_id"],
                    extension=".md",
                )
                stem = Path(filename).stem
            for _export_format, suffix in formats:
                if (directory / f"{stem}{suffix}").is_file():
                    count += 1
    return count
