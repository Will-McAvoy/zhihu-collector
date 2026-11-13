"""导出计划（ExportPlan）数据契约。

统一描述内容范围、顺序、格式、路径、冲突处理策略及来源。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from zhihu_collector.utils.time_utils import now_iso


@dataclass
class ExportPlan:
    plan_id: str = field(default_factory=lambda: f"plan_{uuid.uuid4().hex[:12]}")
    scope_kind: str = "network_plan"  # 'network_plan' | 'offline_exact' | 'collection_exact'
    source_title: str = ""
    source_url: str = ""
    source_id: str = ""
    member_ids: list[str] = field(default_factory=list)
    member_items: list[dict] = field(default_factory=list)
    export_markdown: bool = True
    export_docx: bool = True
    export_pdf: bool = True
    export_separate: bool = True
    export_merged: bool = False
    download_images: bool = True
    save_metadata: bool = True
    output_root: str = ""
    conflict_policy: str = "new_copy"  # 'new_copy' | 'overwrite' | 'skip'
    max_items: int = 0
    collection_snapshot_id: int | None = None
    merged_filename: str = ""
    expected_file_count: int = 0
    existing_target_count: int = 0
    target_count_unknown: bool = True
    created_at: str = field(default_factory=now_iso)

    @property
    def is_exact(self) -> bool:
        return self.scope_kind in ("offline_exact", "collection_exact")

    @property
    def exact_count(self) -> int:
        return len(self.member_ids)

    def validate(self) -> None:
        if not any((self.export_markdown, self.export_docx, self.export_pdf)):
            raise ValueError("至少需要选择一种导出格式（Markdown、Word 或 PDF）")
        if not (self.export_separate or self.export_merged):
            raise ValueError("至少需要勾选「每篇单独文件」或「合并电子书」其中一项")
        if not self.output_root:
            raise ValueError("输出目录不能为空")
        if self.conflict_policy not in ("new_copy", "overwrite", "skip"):
            raise ValueError(f"未知的冲突策略：{self.conflict_policy}")
        if self.is_exact and not self.member_ids:
            raise ValueError("精确导出计划必须包含至少一篇有效内容")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def expected_export_file_count(
    member_count: int,
    *,
    export_markdown: bool,
    export_docx: bool,
    export_pdf: bool,
    export_separate: bool,
    export_merged: bool,
) -> int:
    """按实际会写出的文件计算。只选合并时不把每篇再乘一遍。"""
    formats = sum(1 for enabled in (export_markdown, export_docx, export_pdf) if enabled)
    total = 0
    if export_separate:
        total += max(0, member_count) * formats
    if export_merged:
        total += formats
    return total


def resolve_file_conflict(
    target_path: Path,
    conflict_policy: str = "new_copy",
    batch_suffix: str = "",
) -> tuple[Path, bool]:
    """根据冲突策略处理目标文件路径。
    返回 (最终路径, 是否应跳过写入)。
    """
    if not target_path.exists():
        return target_path, False

    if conflict_policy == "skip":
        return target_path, True

    if conflict_policy == "overwrite":
        return target_path, False

    # conflict_policy == "new_copy"
    parent = target_path.parent
    stem = target_path.stem
    suffix = target_path.suffix

    if batch_suffix:
        candidate = parent / f"{stem}_{batch_suffix}{suffix}"
        if not candidate.exists():
            return candidate, False

    index = 1
    while True:
        candidate = parent / f"{stem}_副本{index}{suffix}"
        if not candidate.exists():
            return candidate, False
        index += 1
