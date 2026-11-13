"""把列表分页绑定到当前任务的目标接口。

作者回答、作者想法、问题回答各自只接受路径和身份都对得上的那一条响应。
其他推荐流、其他作者、相似 ID 或详情接口不能提供 is_end、next 或 totals。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse


from zhihu_collector.utils.urls import is_zhihu_host


_KEEP_QUERY = {"offset", "limit", "cursor", "after_id", "before_id"}
_CREDENTIAL_KEYS = {
    "access_token",
    "token",
    "auth",
    "authorization",
    "cookie",
    "signature",
    "auth_key",
    "x-zst-81",
    "x-zse-96",
    "ossaccesskeyid",
}


@dataclass(frozen=True)
class ListTarget:
    kind: str
    identity: str


def response_matches_list(url: str, target: ListTarget | None) -> bool:
    """路径段必须完整相等。example-22 不会命中 example-2 或 example-220。"""
    if target is None or not target.identity:
        return False
    parsed = urlparse(url or "")
    if parsed.netloc and not is_zhihu_host(parsed.hostname):
        return False
    parts = [unquote(part) for part in (parsed.path or "").split("/") if part]
    identity = target.identity
    if target.kind == "member_answers":
        return parts == ["api", "v4", "members", identity, "answers"]
    if target.kind == "member_pins":
        return parts in (
            ["api", "v4", "members", identity, "pins"],
            ["api", "v4", "v2", "pins", identity, "moments"],
        )
    if target.kind == "question_answers":
        return (
            len(parts) == 5
            and parts[:3] == ["api", "v4", "questions"]
            and parts[3] == identity
            and parts[4] in {"answers", "feeds"}
        )
    return False


def sanitize_endpoint(url: str) -> str:
    """只保留路径和翻页参数。去掉凭证、签名和其它查询参数。"""
    if not url:
        return ""
    parsed = urlparse(str(url))
    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        if key.lower() in _CREDENTIAL_KEYS or key.lower().startswith("x-amz-"):
            continue
        if key.lower() in _KEEP_QUERY:
            kept.append((key, value[:80]))
    path = parsed.path or ""
    query = urlencode(kept)
    return urlunparse(("", "", path, "", query, ""))


def describe_paging(data: Any) -> dict | None:
    """字段缺失和明确终止分开。没有 paging 对象时返回 None。"""
    if not isinstance(data, dict):
        return None
    raw = data.get("paging")
    if not isinstance(raw, dict):
        return None
    is_end_present = "is_end" in raw
    is_end_value = raw.get("is_end")
    if not is_end_present:
        is_end = None
    elif is_end_value is True or is_end_value == 1:
        is_end = True
    elif is_end_value is False or is_end_value == 0:
        is_end = False
    else:
        is_end = None
    next_present = "next" in raw
    if not next_present:
        has_next = None
        next_text = ""
    else:
        next_raw = raw.get("next")
        next_text = sanitize_endpoint(next_raw) if isinstance(next_raw, str) and next_raw.strip() else ""
        has_next = bool(next_text)
    totals = _optional_total(raw.get("totals")) if "totals" in raw else None
    return {
        "is_end": is_end,
        "is_end_present": is_end_present,
        "next_present": next_present,
        "next": next_text,
        "has_next": has_next,
        "totals": totals,
        "totals_present": totals is not None,
    }


def choose_paging(items, target: ListTarget | None, held: dict | None) -> tuple[dict | None, int]:
    """只让目标列表的最后一条分页替换 held。其它列表响应记为忽略。"""
    current = held
    ignored = 0
    for item in items or []:
        url = str(getattr(item, "url", "") or "")
        status = getattr(item, "status", 200)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 200
        if status >= 400:
            continue
        if not response_matches_list(url, target):
            if _foreign_listing(url):
                ignored += 1
            continue
        data = getattr(item, "data", None)
        described = describe_paging(data)
        if described is None and isinstance(getattr(item, "paging", None), dict):
            described = dict(item.paging)
        if described is None:
            continue
        described = dict(described)
        described["url"] = sanitize_endpoint(url)
        described["status"] = status
        current = described
    return current, ignored


def apply_bound_paging(state, items, target: ListTarget | None) -> dict:
    """把本轮已归属的分页写进 ScrollState，并给出 observe_page 要用的停止参数。"""
    held, ignored = choose_paging(items, target, getattr(state, "bound_paging", None))
    state.bound_paging = held
    state.ignored_foreign = int(getattr(state, "ignored_foreign", 0) or 0) + ignored
    is_end = bool(held and held.get("is_end") is True)
    has_next = held.get("has_next") if held else None
    totals = held.get("totals") if held and isinstance(held.get("totals"), int) else None
    if isinstance(totals, int):
        seen = list(getattr(state, "totals_seen", []) or [])
        if not seen or seen[-1] != totals:
            seen.append(totals)
        state.totals_seen = seen
        state.declared_total = totals
    return {
        "is_end": is_end,
        "has_next": has_next,
        "totals": totals,
        "ignored": ignored,
        "held": held,
    }


def expected_item_types(target: ListTarget | None) -> set[str]:
    if target is not None and target.kind == "member_pins":
        return {"pin"}
    return {"answer"}


def summarize_list_items(items, target: ListTarget | None) -> dict:
    """统计目标列表里的原始条目、可识别 ID、无法解析和类型不明。"""
    expected = expected_item_types(target)
    raw = unparsed = unknown = identified = 0
    for item in items or []:
        url = str(getattr(item, "url", "") or "")
        if not response_matches_list(url, target):
            continue
        data = getattr(item, "data", None)
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            raw += 1
            obj = row
            declared_type = ""
            if isinstance(row, dict):
                declared_type = str(row.get("type") or "")
                nested = row.get("target")
                if isinstance(nested, dict):
                    obj = nested
                    declared_type = str(obj.get("type") or declared_type)
            if not isinstance(obj, dict):
                unparsed += 1
                continue
            if declared_type and declared_type not in expected and declared_type not in {"feed", "list_item"}:
                unknown += 1
                continue
            if obj.get("id") in (None, ""):
                unparsed += 1
                continue
            identified += 1
    return {"raw": raw, "identified": identified, "unparsed": unparsed, "unknown": unknown}


def closure_notice(
    *,
    saved_total: int,
    saved_new: int,
    saved_updated: int,
    reused: int,
    scanned: int,
    declared: int | None,
    end_evidence: str,
    totals_seen: list[int] | None = None,
    unparsed: int = 0,
    unknown: int = 0,
    intentional: str = "",
) -> str:
    """差额没有解释时返回提示。可以宣称遍历完成时返回空字符串。"""
    if intentional in {"limit", "incremental"}:
        return ""
    clean = unparsed == 0 and unknown == 0
    counts_agree = declared is None or declared == scanned
    api_closed = end_evidence in {"is_end", "no_next"}
    dom_closed = end_evidence == "dom"
    if clean and counts_agree and (api_closed or dom_closed):
        return ""

    head = (
        f"本次已结束：保存{saved_total}篇"
        f"（本次新增{saved_new}，更新{saved_updated}，复用{reused}）。"
    )
    pieces = [head]
    if api_closed and clean and declared is not None and declared != scanned:
        pieces.append("列表遍历完成。")
        pieces.append(
            f"网站参考总数{declared}，与实际扫描{scanned}相差{abs(declared - scanned)}条，差额尚未核实。"
        )
    elif declared is not None and declared != scanned:
        pieces.append(f"列表参考总数{declared}，差额{abs(declared - scanned)}条尚未核实。")
    else:
        pieces.append(_weak_end_text(end_evidence))
    change = _totals_change_text(totals_seen or [])
    if change:
        pieces.append(change)
    if unparsed:
        pieces.append(f"另有{unparsed}条列表条目未能解析。")
    if unknown:
        pieces.append(f"另有{unknown}条类型未识别。")
    pieces.append("已保存内容可阅读，但无法确认全量完整。")
    return "".join(pieces)


def success_summary(saved_new: int, saved_updated: int, reused: int) -> str:
    return f"搜集完成：本轮新增 {saved_new} 篇，更新 {saved_updated} 篇，跳过 {reused} 篇"


def _weak_end_text(end_evidence: str) -> str:
    if end_evidence == "stale":
        return "列表在连续无新内容后停止，没有可靠末页证据。"
    if end_evidence == "dom":
        return "页面列表区域显示没有更多内容，但接口末页证据不足。"
    return "没有可靠的列表末页证据。"


def _totals_change_text(totals_seen: list[int]) -> str:
    distinct: list[int] = []
    for value in totals_seen:
        if isinstance(value, int) and (not distinct or distinct[-1] != value):
            distinct.append(value)
    if len(set(distinct)) <= 1:
        return ""
    return f"网站参考总数在采集过程中由{distinct[0]}变为{distinct[-1]}，差额按最后一次已归属响应计算。"


def _optional_total(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _foreign_listing(url: str) -> bool:
    path = urlparse(url or "").path
    return any(part in path for part in ("/answers", "/pins", "/feeds", "/collections/", "/favlists/"))
