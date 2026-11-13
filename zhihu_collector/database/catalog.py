"""常用来源、队列、标签和阅读状态。删除来源或队列不会删除正文。"""

from __future__ import annotations

import json
from typing import Any

from zhihu_collector.utils.time_utils import now_iso

OPEN_QUEUE_STATUSES = (
    "waiting",
    "running",
    "paused",
    "awaiting_confirm",
    "needs_login",
    "needs_verify",
    "interrupted",
)
ACTIVE_DEDUPE_STATUSES = (
    "waiting",
    "running",
    "paused",
    "awaiting_confirm",
    "needs_login",
    "needs_verify",
)
QUEUE_STATUS_LABELS = {
    "waiting": "等待中",
    "running": "执行中",
    "paused": "已暂停",
    "awaiting_confirm": "等待范围确认",
    "needs_login": "需要登录",
    "needs_verify": "需要验证",
    "success": "成功",
    "partial": "部分完成",
    "unconfirmed": "完整性未确认",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}


def normalize_tag(value: str) -> str:
    text = " ".join((value or "").replace("\u3000", " ").split())
    return text


def fetch_source(database, source_id: int):
    with database._lock:
        return database._conn.execute("SELECT * FROM sources WHERE id = ?", (int(source_id),)).fetchone()


def source_filter_sql(source) -> tuple[str, list[Any]]:
    """按稳定身份关联已有正文，不用昵称做唯一条件。"""
    kind = source["target_kind"]
    mode = source["collect_mode"]
    identity = source["identity_key"] or ""
    if kind == "user":
        token = identity.rsplit(":", 1)[-1] if ":" in identity else ""
        types = {"answers": ["answer"], "pins": ["pin"], "all": ["answer", "pin"]}.get(mode, ["answer", "pin"])
        marks = ", ".join("?" for _ in types)
        return (
            f"""c.author_id IN (
                SELECT id FROM users
                WHERE url = ? OR rtrim(url, '/') = rtrim(?, '/') OR (zhihu_user_id != '' AND zhihu_user_id = ?)
            ) AND c.content_type IN ({marks})""",
            [source["canonical_url"], source["canonical_url"], token, *types],
        )
    if kind == "question":
        question_id = identity.split(":", 1)[1] if ":" in identity else ""
        return (
            "c.question_id IN (SELECT id FROM questions WHERE zhihu_question_id = ?) AND c.content_type = 'answer'",
            [question_id],
        )
    if kind == "answer":
        answer_id = identity.split(":", 1)[1] if ":" in identity else ""
        return ("c.content_type = 'answer' AND c.zhihu_content_id = ?", [answer_id])
    if kind == "collection":
        collection_id = identity.split(":", 1)[1] if ":" in identity else ""
        return (
            """c.content_type = 'answer' AND c.zhihu_content_id IN (
                SELECT answer_id FROM collection_members WHERE collection_id = ?
            )""",
            [collection_id],
        )
    return ("1 = 0", [])


def count_source_contents(database, source) -> int:
    clause, params = source_filter_sql(source)
    with database._lock:
        row = database._conn.execute(
            f"SELECT COUNT(*) AS value FROM contents c WHERE {clause}",
            params,
        ).fetchone()
    return int(row["value"])


def list_sources(database) -> list:
    with database._lock:
        return list(database._conn.execute("SELECT * FROM sources ORDER BY sort_order, id").fetchall())


def save_source(
    database,
    *,
    display_name: str,
    canonical_url: str,
    target_kind: str,
    identity_key: str,
    collect_mode: str,
    preset_id: str = "",
    preset_snapshot: dict | None = None,
) -> tuple[int, bool]:
    now = now_iso()
    snapshot = json.dumps(preset_snapshot or {}, ensure_ascii=False)
    name = (display_name or "").strip() or canonical_url
    with database._lock:
        existing = database._conn.execute(
            "SELECT id FROM sources WHERE identity_key = ? AND collect_mode = ?",
            (identity_key, collect_mode),
        ).fetchone()
        if existing is not None:
            return int(existing["id"]), False
        order = database._conn.execute("SELECT COALESCE(MAX(sort_order), 0) AS value FROM sources").fetchone()
        cursor = database._conn.execute(
            """
            INSERT INTO sources(
                display_name, canonical_url, target_kind, identity_key, collect_mode,
                preset_id, preset_snapshot_json, sort_order, created_at, last_status, last_result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '{}')
            """,
            (
                name,
                canonical_url,
                target_kind,
                identity_key,
                collect_mode,
                preset_id or "",
                snapshot,
                int(order["value"]) + 1,
                now,
            ),
        )
        database._conn.commit()
        return int(cursor.lastrowid), True


def update_source(
    database,
    source_id: int,
    *,
    display_name: str | None = None,
    preset_id: str | None = None,
    preset_snapshot: dict | None = None,
    collect_mode: str | None = None,
) -> None:
    fields: dict[str, Any] = {}
    if display_name is not None:
        fields["display_name"] = display_name.strip() or ""
    if preset_id is not None:
        fields["preset_id"] = preset_id
    if preset_snapshot is not None:
        fields["preset_snapshot_json"] = json.dumps(preset_snapshot, ensure_ascii=False)
    if collect_mode is not None:
        fields["collect_mode"] = collect_mode
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    with database._lock:
        database._conn.execute(
            f"UPDATE sources SET {columns} WHERE id = ?",
            (*fields.values(), int(source_id)),
        )
        database._conn.commit()


def move_source_order(database, source_id: int, direction: int) -> bool:
    if direction not in {-1, 1}:
        return False
    with database._lock:
        sources = list(database._conn.execute("SELECT id, sort_order FROM sources ORDER BY sort_order, id").fetchall())
        idx = next((i for i, s in enumerate(sources) if int(s["id"]) == int(source_id)), None)
        if idx is None:
            return False
        target_idx = idx + direction
        if target_idx < 0 or target_idx >= len(sources):
            return False
        sources[idx], sources[target_idx] = sources[target_idx], sources[idx]
        for pos, s in enumerate(sources, start=1):
            database._conn.execute("UPDATE sources SET sort_order = ? WHERE id = ?", (pos, int(s["id"])))
        database._conn.commit()
        return True


def delete_source(database, source_id: int) -> None:
    with database._lock:
        database._conn.execute("DELETE FROM sources WHERE id = ?", (int(source_id),))
        database._conn.commit()


def note_source_result(
    database,
    source_id: int,
    *,
    status: str,
    task_id: int | None,
    result: dict,
    succeeded: bool,
    learned_name: str = "",
) -> None:
    now = now_iso()
    with database._lock:
        row = database._conn.execute("SELECT * FROM sources WHERE id = ?", (int(source_id),)).fetchone()
        if row is None:
            return
        name = row["display_name"]
        learned = (learned_name or "").strip()
        if learned and (not name or name == row["canonical_url"] or str(name).startswith("http")):
            name = learned
        database._conn.execute(
            """
            UPDATE sources
            SET display_name = ?, last_attempt_at = ?, last_status = ?, last_result_json = ?,
                last_task_id = ?, last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END
            WHERE id = ?
            """,
            (
                name,
                now,
                status,
                json.dumps(result, ensure_ascii=False),
                task_id,
                1 if succeeded else 0,
                now,
                int(source_id),
            ),
        )
        database._conn.commit()


def record_task_content(database, task_id: int, content_id: int, outcome: str) -> None:
    if not task_id or not content_id:
        return
    with database._lock:
        database._conn.execute(
            """
            INSERT INTO task_content_results(task_id, content_id, outcome)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id, content_id) DO UPDATE SET outcome = excluded.outcome
            """,
            (int(task_id), int(content_id), outcome),
        )
        database._conn.commit()


def task_content_ids(database, task_id: int, outcome: str = "") -> list[int]:
    query = "SELECT content_id FROM task_content_results WHERE task_id = ?"
    params: list[Any] = [int(task_id)]
    if outcome:
        if outcome in ("updated", "body_updated"):
            query += " AND outcome IN ('updated', 'body_updated')"
        elif outcome in ("metadata", "metadata_updated"):
            query += " AND outcome IN ('metadata', 'metadata_updated')"
        else:
            query += " AND outcome = ?"
            params.append(outcome)
    query += " ORDER BY id"
    with database._lock:
        rows = database._conn.execute(query, params).fetchall()
    return [int(row["content_id"]) for row in rows]


def set_read_state(database, content_ids: list[int], *, is_read: bool) -> int:
    now = now_iso()
    changed = 0
    with database._lock:
        for content_id in content_ids:
            database._conn.execute(
                """
                INSERT INTO content_user_state(content_id, is_read, starred, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(content_id) DO UPDATE SET is_read = excluded.is_read, updated_at = excluded.updated_at
                """,
                (int(content_id), 1 if is_read else 0, now),
            )
            changed += 1
        database._conn.commit()
    return changed


def set_starred(database, content_ids: list[int], *, starred: bool) -> int:
    now = now_iso()
    with database._lock:
        for content_id in content_ids:
            database._conn.execute(
                """
                INSERT INTO content_user_state(content_id, is_read, starred, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(content_id) DO UPDATE SET starred = excluded.starred, updated_at = excluded.updated_at
                """,
                (int(content_id), 1 if starred else 0, now),
            )
        database._conn.commit()
    return len(content_ids)


def content_tags(database, content_id: int) -> list[str]:
    with database._lock:
        rows = database._conn.execute(
            """
            SELECT t.name FROM tags t
            JOIN content_tags ct ON ct.tag_id = t.id
            WHERE ct.content_id = ?
            ORDER BY t.name
            """,
            (int(content_id),),
        ).fetchall()
    return [row["name"] for row in rows]


def list_tag_names(database) -> list[str]:
    with database._lock:
        rows = database._conn.execute("SELECT name FROM tags ORDER BY name").fetchall()
    return [row["name"] for row in rows]


def add_tags(database, content_ids: list[int], names: list[str]) -> list[str]:
    cleaned = []
    seen = set()
    for name in names:
        text = normalize_tag(name)
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    if not cleaned or not content_ids:
        return cleaned
    now = now_iso()
    with database._lock:
        for name in cleaned:
            database._conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
        tag_ids = [
            int(database._conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"])
            for name in cleaned
        ]
        for content_id in content_ids:
            for tag_id in tag_ids:
                database._conn.execute(
                    "INSERT OR IGNORE INTO content_tags(content_id, tag_id) VALUES (?, ?)",
                    (int(content_id), tag_id),
                )
            database._conn.execute(
                """
                INSERT INTO content_user_state(content_id, is_read, starred, updated_at)
                VALUES (?, 0, 0, ?)
                ON CONFLICT(content_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (int(content_id), now),
            )
        database._conn.commit()
    return cleaned


def remove_tags(database, content_ids: list[int], names: list[str]) -> None:
    cleaned = [normalize_tag(name) for name in names]
    cleaned = [name for name in cleaned if name]
    if not cleaned or not content_ids:
        return
    with database._lock:
        for name in cleaned:
            row = database._conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            if row is None:
                continue
            marks = ", ".join("?" for _ in content_ids)
            database._conn.execute(
                f"DELETE FROM content_tags WHERE tag_id = ? AND content_id IN ({marks})",
                (int(row["id"]), *[int(item) for item in content_ids]),
            )
        database._conn.commit()


def user_state(database, content_id: int) -> tuple[bool, bool, list[str]]:
    with database._lock:
        row = database._conn.execute(
            "SELECT is_read, starred FROM content_user_state WHERE content_id = ?",
            (int(content_id),),
        ).fetchone()
    tags = content_tags(database, content_id)
    if row is None:
        return False, False, tags
    return bool(row["is_read"]), bool(row["starred"]), tags


def enqueue_requests(database, items: list[dict], *, batch_id: str) -> list[int]:
    """items: request_json, dedupe_key, source_id。内部事务保证活动任务去重。"""
    now = now_iso()
    ids = []
    with database._lock:
        active = active_queue_by_key(database)
        active_keys = set(active.keys())
        current = database._conn.execute("SELECT COALESCE(MAX(position), 0) AS value FROM queue_items").fetchone()
        position = int(current["value"])
        for item in items:
            key = item.get("dedupe_key") or ""
            if key and key in active_keys:
                continue
            if key:
                active_keys.add(key)
            position += 1
            cursor = database._conn.execute(
                """
                INSERT INTO queue_items(
                    batch_id, position, status, dedupe_key, request_json, source_id,
                    error_message, result_json, created_at
                ) VALUES (?, ?, 'waiting', ?, ?, ?, '', '{}', ?)
                """,
                (
                    batch_id,
                    position,
                    item["dedupe_key"],
                    item["request_json"],
                    item.get("source_id"),
                    now,
                ),
            )
            ids.append(int(cursor.lastrowid))
        database._conn.commit()
    return ids


def list_queue_items(database) -> list:
    with database._lock:
        return list(
            database._conn.execute(
                "SELECT * FROM queue_items ORDER BY position, id"
            ).fetchall()
        )


def active_queue_by_key(database) -> dict[str, Any]:
    marks = ", ".join("?" for _ in ACTIVE_DEDUPE_STATUSES)
    with database._lock:
        rows = database._conn.execute(
            f"SELECT * FROM queue_items WHERE status IN ({marks}) ORDER BY position, id",
            ACTIVE_DEDUPE_STATUSES,
        ).fetchall()
    found = {}
    for row in rows:
        found.setdefault(row["dedupe_key"], row)
    return found


def count_open_queue(database) -> int:
    marks = ", ".join("?" for _ in OPEN_QUEUE_STATUSES)
    with database._lock:
        row = database._conn.execute(
            f"SELECT COUNT(*) AS value FROM queue_items WHERE status IN ({marks})",
            OPEN_QUEUE_STATUSES,
        ).fetchone()
    return int(row["value"])


def claim_next_queue_item(database, *, include_interrupted: bool = False):
    statuses = ["waiting"]
    if include_interrupted:
        statuses.append("interrupted")
    marks = ", ".join("?" for _ in statuses)
    with database._lock:
        database._conn.execute("BEGIN IMMEDIATE")
        blocking = database._conn.execute(
            """
            SELECT id, status FROM queue_items
            WHERE status IN ('needs_login', 'needs_verify')
            LIMIT 1
            """
        ).fetchone()
        if blocking is not None:
            database._conn.commit()
            return None
        row = database._conn.execute(
            f"""
            SELECT * FROM queue_items
            WHERE status IN ({marks})
            ORDER BY position, id
            LIMIT 1
            """,
            statuses,
        ).fetchone()
        if row is None:
            database._conn.commit()
            return None
        updated = database._conn.execute(
            """
            UPDATE queue_items
            SET status = 'running', started_at = ?, error_message = ''
            WHERE id = ? AND status = ?
            """,
            (now_iso(), int(row["id"]), row["status"]),
        )
        database._conn.commit()
        if updated.rowcount != 1:
            return None
        return database._conn.execute("SELECT * FROM queue_items WHERE id = ?", (int(row["id"]),)).fetchone()


def update_queue_item(database, item_id: int, **fields) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    with database._lock:
        database._conn.execute(
            f"UPDATE queue_items SET {columns} WHERE id = ?",
            (*fields.values(), int(item_id)),
        )
        database._conn.commit()


def finish_queue_item(database, item_id: int, status: str, error: str = "", result: dict | None = None) -> bool:
    with database._lock:
        current = database._conn.execute("SELECT status FROM queue_items WHERE id = ?", (int(item_id),)).fetchone()
        if current is None or current["status"] != "running":
            return False
        database._conn.execute(
            """
            UPDATE queue_items
            SET status = ?, error_message = ?, result_json = ?, finished_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (status, error or "", json.dumps(result or {}, ensure_ascii=False), now_iso(), int(item_id)),
        )
        database._conn.commit()
        return True


def delete_waiting_item(database, item_id: int) -> bool:
    with database._lock:
        cursor = database._conn.execute(
            "DELETE FROM queue_items WHERE id = ? AND status = 'waiting'",
            (int(item_id),),
        )
        database._conn.commit()
        return cursor.rowcount == 1


def move_waiting_item(database, item_id: int, direction: int) -> bool:
    if direction not in {-1, 1}:
        return False
    with database._lock:
        current = database._conn.execute(
            "SELECT * FROM queue_items WHERE id = ? AND status = 'waiting'",
            (int(item_id),),
        ).fetchone()
        if current is None:
            return False
        if direction < 0:
            neighbor = database._conn.execute(
                """
                SELECT * FROM queue_items
                WHERE status = 'waiting' AND (position < ? OR (position = ? AND id < ?))
                ORDER BY position DESC, id DESC LIMIT 1
                """,
                (current["position"], current["position"], current["id"]),
            ).fetchone()
        else:
            neighbor = database._conn.execute(
                """
                SELECT * FROM queue_items
                WHERE status = 'waiting' AND (position > ? OR (position = ? AND id > ?))
                ORDER BY position, id LIMIT 1
                """,
                (current["position"], current["position"], current["id"]),
            ).fetchone()
        if neighbor is None:
            return False
        database._conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (neighbor["position"], current["id"]))
        database._conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (current["position"], neighbor["id"]))
        database._conn.commit()
        return True


SUPERSEDED_MESSAGE = "同一目标已有其他队列项，本项不再重复执行"


def reopen_queue_statuses(database, statuses: tuple[str, ...]) -> int:
    """重新排队。同一目标已有活动项时，旧项标为已取消，不再生成第二条等待项。"""
    if not statuses:
        return 0
    marks = ", ".join("?" for _ in statuses)
    reopened = 0
    with database._lock:
        rows = database._conn.execute(
            f"SELECT id, status, dedupe_key FROM queue_items WHERE status IN ({marks}) ORDER BY position, id",
            statuses,
        ).fetchall()
        active_keys = {
            row["dedupe_key"]
            for row in database._conn.execute(
                f"SELECT dedupe_key FROM queue_items WHERE status IN ({', '.join('?' for _ in ACTIVE_DEDUPE_STATUSES)})",
                ACTIVE_DEDUPE_STATUSES,
            ).fetchall()
            if row["dedupe_key"]
        }
        for row in rows:
            key = row["dedupe_key"] or ""
            # 本身就是活动状态（需要登录/验证）的项占着自己的键，直接恢复
            if key and key in active_keys and row["status"] not in ACTIVE_DEDUPE_STATUSES:
                database._conn.execute(
                    "UPDATE queue_items SET status = 'cancelled', error_message = ?, finished_at = ? WHERE id = ?",
                    (SUPERSEDED_MESSAGE, now_iso(), int(row["id"])),
                )
                continue
            if key:
                active_keys.add(key)
            database._conn.execute(
                "UPDATE queue_items SET status = 'waiting', finished_at = NULL WHERE id = ?",
                (int(row["id"]),),
            )
            reopened += 1
        database._conn.commit()
    return reopened


def retry_failed_item(database, item_id: int) -> bool:
    """失败项重新排队；同一目标已在队列中时不重复排队，返回 False。"""
    with database._lock:
        row = database._conn.execute(
            "SELECT dedupe_key FROM queue_items WHERE id = ? AND status = 'failed'",
            (int(item_id),),
        ).fetchone()
        if row is None:
            return False
        key = row["dedupe_key"] or ""
        if key:
            marks = ", ".join("?" for _ in ACTIVE_DEDUPE_STATUSES)
            duplicate = database._conn.execute(
                f"SELECT 1 FROM queue_items WHERE dedupe_key = ? AND id <> ? AND status IN ({marks}) LIMIT 1",
                (key, int(item_id), *ACTIVE_DEDUPE_STATUSES),
            ).fetchone()
            if duplicate is not None:
                return False
        cursor = database._conn.execute(
            """
            UPDATE queue_items
            SET status = 'waiting', error_message = '', finished_at = NULL
            WHERE id = ? AND status = 'failed'
            """,
            (int(item_id),),
        )
        database._conn.commit()
        return cursor.rowcount == 1


def task_open_body_failures(database, task_id: int) -> int:
    with database._lock:
        row = database._conn.execute(
            "SELECT COUNT(*) AS value FROM content_failures WHERE task_id = ? AND status = 'failed'",
            (int(task_id),),
        ).fetchone()
    return int(row["value"]) if row else 0


def queue_counts(database) -> dict[str, int]:
    with database._lock:
        rows = database._conn.execute(
            "SELECT status, COUNT(*) AS value FROM queue_items GROUP BY status"
        ).fetchall()
    return {row["status"]: int(row["value"]) for row in rows}
