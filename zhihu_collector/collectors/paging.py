"""翻页停止条件。连续没有新内容、接口声明结束、或没有下一页时停止。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScrollState:
    max_stale_rounds: int = 5
    seen_ids: set[str] = field(default_factory=set)
    emitted_ids: set[str] = field(default_factory=set)
    skipped_ids: set[str] = field(default_factory=set)
    last_skipped_count: int = 0
    stale_rounds: int = 0
    reached_end: bool = False
    stop_reason: str = ""
    end_evidence: str = ""
    declared_total: int | None = None
    totals_seen: list[int] = field(default_factory=list)
    bound_paging: dict | None = None
    ignored_foreign: int = 0
    duplicate_count: int = 0
    unparsed_count: int = 0
    unknown_type_count: int = 0
    saved_total: int = 0

    def observe_page(
        self,
        content_ids: list[str],
        *,
        is_end: bool = False,
        has_next: bool | None = None,
        page_says_no_more: bool = False,
        unchanged_ids: set[str] | None = None,
        update_only: bool = False,
        sorted_by_time: bool = False,
    ) -> list[str]:
        """返回本次扫描第一次见到的 content id。

        旧内容第一次出现也算翻页有进展。只有整页都是已见过的 ID，才累计无进展次数。
        """
        fresh: list[str] = []
        for content_id in content_ids:
            if not content_id or content_id in self.emitted_ids:
                continue
            self.emitted_ids.add(content_id)
            self.seen_ids.add(content_id)
            fresh.append(content_id)
        if fresh:
            self.stale_rounds = 0
        else:
            self.stale_rounds += 1
        page_ids = {content_id for content_id in content_ids if content_id}
        if is_end:
            self.reached_end = True
            self.stop_reason = "分页已经结束：接口声明已到末页"
            self.end_evidence = "is_end"
        elif has_next is False:
            self.reached_end = True
            self.stop_reason = "分页已经结束：接口没有下一页"
            self.end_evidence = "no_next"
        elif (
            update_only
            and sorted_by_time
            and page_ids
            and unchanged_ids is not None
            and page_ids.issubset(unchanged_ids)
        ):
            self.reached_end = True
            self.stop_reason = "按时间排序后这一页都没有变化，增量更新结束"
            self.end_evidence = "incremental"
        elif page_says_no_more and self.stale_rounds >= 2:
            self.reached_end = True
            self.stop_reason = "页面列表区域显示没有更多内容"
            self.end_evidence = "dom"
        elif self.stale_rounds >= self.max_stale_rounds:
            self.reached_end = True
            self.stop_reason = f"连续 {self.max_stale_rounds} 次没有新内容"
            self.end_evidence = "stale"
        return fresh
