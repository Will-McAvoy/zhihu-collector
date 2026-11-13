"""把多行文本识别成可入队的知乎目标。这一步不联网。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from zhihu_collector.utils.urls import ParsedUrl, parse_zhihu_url

_URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）)】\]]+", re.IGNORECASE)
_MODE_LABELS = {
    "answers": "答主回答",
    "pins": "答主想法",
    "all": "答主全部内容",
    "question": "问题全部回答",
    "single_answer": "仅此回答",
    "collection": "收藏夹回答",
}
_KIND_LABELS = {
    "user": "答主",
    "question": "问题",
    "answer": "回答",
    "collection": "收藏夹",
    "unknown": "无法识别",
}


@dataclass
class BatchTarget:
    line_no: int
    raw: str
    canonical: str = ""
    kind: str = "unknown"
    mode: str = ""
    valid: bool = False
    reason: str = ""
    dedupe_key: str = ""
    identity_key: str = ""
    overlap_note: str = ""
    parsed: ParsedUrl | None = field(default=None, repr=False)

    @property
    def type_label(self) -> str:
        return _KIND_LABELS.get(self.kind, self.kind or "无法识别")

    @property
    def mode_label(self) -> str:
        return _MODE_LABELS.get(self.mode, "未指定" if not self.mode else self.mode)


def _clean_url(value: str) -> str:
    return value.strip().strip("，。；、,;）)】]>\"'")


def _urls_in_line(line: str) -> list[str]:
    found = [_clean_url(item) for item in _URL_RE.findall(line or "")]
    if found:
        return [item for item in found if item]
    token = _clean_url(line or "")
    if token.startswith("www.") or "zhihu.com" in token or token.startswith("zhihu.com"):
        return [token if "://" in token else f"https://{token}"]
    return []


def identity_for(parsed: ParsedUrl, mode: str) -> tuple[str, str, str]:
    """返回 identity_key、实际模式、失败原因。回答链接默认保持单篇，不会扩成整个问题。"""
    resolved = mode or "auto"
    if resolved == "auto":
        resolved = {
            "user": "all",
            "question": "question",
            "answer": "single_answer",
            "collection": "collection",
        }.get(parsed.kind, "")
    if resolved == "single_answer":
        if parsed.kind != "answer" or not parsed.answer_id:
            return "", resolved, "单篇回答需要回答链接，不能把问题或答主猜成一篇回答"
        return f"answer:{parsed.answer_id}", resolved, ""
    if resolved == "question":
        if not parsed.question_id:
            return "", resolved, "问题采集需要问题链接，或带问题编号的回答链接"
        return f"question:{parsed.question_id}", resolved, ""
    if resolved in {"answers", "pins", "all"}:
        if parsed.kind != "user" or not parsed.user_token:
            return "", resolved, "答主采集需要用户主页链接"
        return f"user:{parsed.user_kind}:{parsed.user_token}", resolved, ""
    if resolved == "collection":
        if parsed.kind != "collection" or not parsed.collection_id:
            return "", resolved, "收藏夹采集需要收藏夹链接"
        return f"collection:{parsed.collection_id}", resolved, ""
    return "", resolved, "无法识别这个目标"


def parse_batch(
    text: str,
    *,
    default_mode: str = "auto",
    active_keys: dict | None = None,
    option_signature: dict | None = None,
) -> list[BatchTarget]:
    active_keys = active_keys or {}
    option_signature = option_signature or {}
    items: list[BatchTarget] = []
    seen: dict[str, int] = {}
    for line_no, line in enumerate((text or "").splitlines(), start=1):
        raw = line.strip()
        if not raw:
            items.append(BatchTarget(line_no=line_no, raw="", reason="空行"))
            continue
        urls = _urls_in_line(raw)
        parsed_urls = [(url, parse_zhihu_url(url)) for url in urls]
        supported = [(url, parsed) for url, parsed in parsed_urls if parsed.kind != "unknown"]
        if not urls:
            items.append(BatchTarget(
                line_no=line_no,
                raw=raw,
                reason="没有找到链接。专栏、短链或无法识别的地址不会被猜成作者或问题",
            ))
            continue
        if not supported:
            items.append(BatchTarget(
                line_no=line_no,
                raw=raw,
                canonical=parsed_urls[0][1].canonical,
                reason="无法识别。专栏、短链或其他网站不会被当作答主、问题、回答或收藏夹",
            ))
            continue
        distinct = []
        distinct_keys = set()
        for url, parsed in supported:
            identity, mode, error = identity_for(parsed, default_mode)
            marker = identity or url
            if marker in distinct_keys:
                continue
            distinct_keys.add(marker)
            distinct.append((url, parsed, identity, mode, error))
        if len(distinct) > 1:
            items.append(BatchTarget(
                line_no=line_no,
                raw=raw,
                reason="这一行有多个不同目标，请拆成多行",
            ))
            continue
        _url, parsed, identity, mode, error = distinct[0]
        item = BatchTarget(
            line_no=line_no,
            raw=raw,
            canonical=parsed.canonical,
            kind=parsed.kind,
            mode=mode,
            parsed=parsed,
            identity_key=identity,
        )
        if error or not identity:
            item.reason = error or "无法识别"
            items.append(item)
            continue
        item.dedupe_key = f"{identity}|{mode}"
        previous = seen.get(item.dedupe_key)
        if previous is not None:
            item.reason = f"和第 {previous} 行是同一个目标和范围"
            items.append(item)
            continue
        queued = active_keys.get(item.dedupe_key)
        if queued is not None:
            item.reason = _queue_duplicate_reason(queued, option_signature)
            items.append(item)
            continue
        item.valid = True
        item.reason = "可以加入队列"
        seen[item.dedupe_key] = line_no
        items.append(item)
    _mark_overlap(items)
    return items


def _queue_duplicate_reason(queued, signature: dict) -> str:
    try:
        import json
        stored = json.loads(queued["request_json"] or "{}")
    except Exception:
        stored = {}
    diffs = []
    if "max_items" in signature and int(stored.get("max_items") or 0) != int(signature.get("max_items") or 0):
        diffs.append("每个目标的篇数上限")
    if "execution_intent" in signature and (stored.get("execution_intent") or "") != (signature.get("execution_intent") or ""):
        diffs.append("更新内容库或更新并导出")
    if diffs:
        return "已在队列。队列里保存的是入队时的设置（" + "、".join(diffs) + "不同），这次不会改写它，也不会重复加入"
    return "已在队列，未重复加入"


def history_source_candidates(database) -> list[dict]:
    """从任务历史抽出可导入的来源。同一身份和范围只保留最近一条。"""
    mapping = {
        "user_answers": "answers",
        "user_pins": "pins",
        "user_all": "all",
        "question_answers": "question",
        "single_answer": "single_answer",
        "collection_answers": "collection",
    }
    found: dict[tuple[str, str], dict] = {}
    for task in database.list_tasks():
        parsed = parse_zhihu_url(task["target_url"] or "")
        mode = mapping.get(task["task_type"] or "", "auto")
        identity, resolved, error = identity_for(parsed, mode)
        if error or not identity:
            continue
        key = (identity, resolved)
        current = found.get(key)
        stamp = task["updated_at"] or task["created_at"] or ""
        if current is not None and stamp < current["updated_at"]:
            continue
        found[key] = {
            "identity_key": identity,
            "collect_mode": resolved,
            "canonical_url": parsed.canonical,
            "target_kind": parsed.kind,
            "updated_at": stamp,
            "task_id": int(task["id"]),
            "task_type": task["task_type"] or "",
            "options_json": task["options_json"] or "{}",
        }
    return list(found.values())


def _mark_overlap(items: list[BatchTarget]) -> None:
    groups: dict[str, list[BatchTarget]] = {}
    for item in items:
        if item.valid and item.identity_key.startswith("user:"):
            groups.setdefault(item.identity_key, []).append(item)
    for group in groups.values():
        modes = {item.mode for item in group}
        if "all" in modes and ({"answers", "pins"} & modes):
            for item in group:
                if item.mode in {"answers", "pins"}:
                    item.overlap_note = "范围和「全部内容」有重叠，仍按你选择的范围分别入队"
                    item.reason = item.overlap_note
