"""知乎时间戳转换。接口有时返回秒，有时返回毫秒。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 中国没有夏令时。用固定东八区，避免 Windows 精简版 Python 缺少 tzdata。
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def normalize_unix(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if number <= 0:
        return None
    if number > 10_000_000_000:
        number //= 1000
    if number > 253_402_271_999:  # 9999-12-31 23:59:59 UTC+8
        return None
    return number


def unix_to_iso(value: object) -> str | None:
    number = normalize_unix(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, SHANGHAI).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")
