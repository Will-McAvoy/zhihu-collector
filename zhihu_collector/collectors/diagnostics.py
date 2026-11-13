"""任务级分页诊断。只记计数、端点和结束原因，不记正文、Cookie 或授权信息。"""

from __future__ import annotations

import json
from pathlib import Path

from zhihu_collector.utils.list_target import _CREDENTIAL_KEYS


def append_jsonl(directory: Path, task_id: int, row: dict, *, max_lines: int = 2000, max_age_days: int = 14) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _prune_old(directory, max_age_days)
    path = directory / f"task-{int(task_id)}.jsonl"
    safe = _scrub(row)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")
    _trim(path, max_lines)
    return path


def _scrub(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in _CREDENTIAL_KEYS or str(key).lower() in {"cookie", "authorization", "set-cookie"}:
                continue
            cleaned[str(key)] = _scrub(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub(item) for item in value[:5000]]
    if isinstance(value, str) and _looks_secret(value):
        return ""
    return value


def _looks_secret(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("bearer ") or "access_token=" in lowered or "z_c0=" in lowered


def _prune_old(directory: Path, max_age_days: int) -> None:
    if max_age_days <= 0:
        return
    import time

    cutoff = time.time() - max_age_days * 86400
    for path in directory.glob("task-*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _trim(path: Path, max_lines: int) -> None:
    if max_lines <= 0:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= max_lines:
        return
    path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
