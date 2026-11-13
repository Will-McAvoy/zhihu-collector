"""内容、图片和任务的 SQLite 访问。"""

from __future__ import annotations

import json
import os
import inspect
from functools import wraps
from contextlib import nullcontext
import sqlite3
import threading
from pathlib import Path
from typing import Any

from zhihu_collector.database.migrations import MIGRATIONS
from zhihu_collector.models import AuthorInfo, ContentRecord
from zhihu_collector.utils.artifacts import artifact_problem, file_digest, image_problem
from zhihu_collector.utils.time_utils import now_iso, unix_to_iso

# 导出与正文失败共用的单调事件序号。写在同一条 SQL 里，多个连接并发时也由 SQLite 写锁保证不重复。
_NEXT_EVENT_SEQ = """(SELECT COALESCE(MAX(value), 0) + 1 FROM (
    SELECT MAX(event_seq) AS value FROM exports
    UNION ALL SELECT MAX(event_seq) FROM content_failures))"""


def like_pattern(keyword: str) -> str:
    """普通关键词搜索：% 和 _ 是字面字符，不是 SQL 通配符。配合 ESCAPE '\\' 使用。"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def target_key(path):
    return os.path.normcase(str(Path(path).resolve()))


_CONTENT_SORTS = {
    "id_desc": "c.id DESC",
    "published_desc": "COALESCE(c.created_time, '') DESC, c.id DESC",
    "published_asc": "COALESCE(c.created_time, '') ASC, c.id ASC",
    "checked_desc": "COALESCE(c.last_checked_time, '') DESC, c.id DESC",
    "checked_asc": "COALESCE(c.last_checked_time, '') ASC, c.id ASC",
    "votes_desc": "COALESCE(c.vote_count, -1) DESC, c.id DESC",
    "votes_asc": "COALESCE(c.vote_count, -1) ASC, c.id ASC",
}


_EXPORT_LOCKS = {}
_EXPORT_LOCKS_GUARD = threading.Lock()


def serialized_export(function):
    """Serialize file replacement + state update for one database in this process."""
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        database = bound.get('database') or getattr(bound.get('self'), 'database', None)
        with database._export_lock if database is not None else nullcontext():
            return function(*args, **kwargs)
    return wrapped


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with _EXPORT_LOCKS_GUARD:
            self._export_lock = _EXPORT_LOCKS.setdefault(target_key(self.path), threading.RLock())
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            current = 0
            table = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if table is not None:
                row = self._conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
                current = int(row["version"] or 0)
            backed_up = False
            if current and current < max(v for v, _ in MIGRATIONS):
                self._backup_before_change(f'pre-v{current}')
                backed_up = True
            for version, script in MIGRATIONS:
                if version <= current:
                    continue
                try:
                    self._conn.executescript('BEGIN IMMEDIATE;\n' + script)
                except Exception:
                    self._conn.rollback()
                    raise
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, now_iso()),
                )
                self._conn.commit()
            self._ensure_compatible_columns(backup=bool(current) and not backed_up)
            if self._conn.execute("SELECT 1 FROM exports WHERE target_key='' LIMIT 1").fetchone():
                rows = self._conn.execute('SELECT id, output_path FROM exports').fetchall()
                for row in rows:
                    self._conn.execute('UPDATE exports SET target_key=? WHERE id=?', (target_key(row['output_path']), row['id']))
                # Only automatically supersede an old failure when a later success proves replacement.
                self._conn.execute("""UPDATE exports AS old SET status='superseded'
                    WHERE old.status='failed' AND EXISTS (
                        SELECT 1 FROM exports newer WHERE newer.target_key=old.target_key
                        AND newer.format=old.format AND newer.status='success'
                        AND (newer.updated_at > old.updated_at OR (newer.updated_at=old.updated_at AND newer.id>old.id)))""")
            table = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_content_results'"
            ).fetchone()
            if table is not None:
                self._conn.execute("UPDATE task_content_results SET outcome = 'updated' WHERE outcome = 'body_updated'")
                self._conn.execute("UPDATE task_content_results SET outcome = 'metadata' WHERE outcome = 'metadata_updated'")
            self._conn.commit()

    def _backup_before_change(self, label: str) -> Path:
        # SQLite backup includes WAL pages; preserve a verifiable pre-migration copy.
        import time
        backup_path = self.path.with_name(self.path.name + f'.{label}-{time.time_ns()}.bak')
        with sqlite3.connect(backup_path) as backup:
            self._conn.backup(backup)
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('数据库迁移前备份校验失败')
        return backup_path

    def _ensure_compatible_columns(self, *, backup: bool) -> None:
        """Schema 仍为 V7：只追加可为空的列，旧版本程序打开同一数据库时会忽略它们。

        event_seq 给导出与正文失败提供跨表的先后顺序（同一秒内也能区分）；
        file_size/file_sha256 记录导出产物本身，用来识别空文件、损坏和外部修改。
        """
        wanted = {
            "exports": (("event_seq", "INTEGER"), ("file_size", "INTEGER"), ("file_sha256", "TEXT")),
            "content_failures": (("event_seq", "INTEGER"),),
        }
        missing = []
        for table, columns in wanted.items():
            present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not present:
                continue
            missing.extend((table, name, kind) for name, kind in columns if name not in present)
        if not missing:
            return
        if backup:
            self._backup_before_change('pre-ext1')
        try:
            self._conn.execute('BEGIN IMMEDIATE')
            for table, name, kind in missing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def recover_interrupted_tasks(self) -> None:
        now = now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE tasks
                SET status = 'interrupted',
                    updated_at = ?,
                    error_message = COALESCE(NULLIF(error_message, ''), '程序上次未正常结束')
                WHERE status IN ('running', 'paused')
                """,
                (now,),
            )
            table = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='queue_items'"
            ).fetchone()
            if table is not None:
                self._conn.execute(
                    """
                    UPDATE queue_items
                    SET status = 'interrupted',
                        finished_at = ?,
                        error_message = CASE
                            WHEN status = 'awaiting_confirm' THEN '等待确认时程序退出，需重新扫描或确认'
                            WHEN status = 'paused' THEN '程序上次暂停未完成'
                            WHEN error_message = '' THEN '程序上次未正常结束'
                            ELSE error_message
                        END
                    WHERE status IN ('running', 'paused', 'awaiting_confirm')
                    """,
                    (now,),
                )
            self._conn.commit()

    def upsert_user(self, author: AuthorInfo) -> int | None:
        if not author or (not author.name and not author.url_token and not author.zhihu_user_id):
            return None
        if author.name == "匿名用户" or author.zhihu_user_id in {"", "0"} and not author.url_token:
            return self._anonymous_user_id()
        now = now_iso()
        with self._lock:
            row = None
            if author.zhihu_user_id and author.zhihu_user_id != "0":
                row = self._conn.execute(
                    "SELECT id FROM users WHERE zhihu_user_id = ?",
                    (author.zhihu_user_id,),
                ).fetchone()
            if row is None and author.url:
                row = self._conn.execute("SELECT id FROM users WHERE url = ?", (author.url,)).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO users(zhihu_user_id, name, headline, url, avatar_url, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (author.zhihu_user_id, author.name, author.headline, author.url, author.avatar_url, now, now),
                )
                self._conn.commit()
                return int(cursor.lastrowid)
            # 响应缺少某个字段时保留已知资料；只有明确的新值才覆盖。
            self._conn.execute(
                """
                UPDATE users
                SET name = COALESCE(NULLIF(?, ''), name),
                    headline = COALESCE(NULLIF(?, ''), headline),
                    url = COALESCE(NULLIF(?, ''), url),
                    avatar_url = COALESCE(NULLIF(?, ''), avatar_url),
                    updated_at = ?,
                    zhihu_user_id = CASE WHEN zhihu_user_id IS NULL OR zhihu_user_id = '' THEN ? ELSE zhihu_user_id END
                WHERE id = ?
                """,
                (author.name, author.headline, author.url, author.avatar_url, now, author.zhihu_user_id, row["id"]),
            )
            self._conn.commit()
            return int(row["id"])

    def _anonymous_user_id(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM users WHERE name = ? AND (url IS NULL OR url = '')",
                ("匿名用户",),
            ).fetchone()
            if row is not None:
                return int(row["id"])
            now = now_iso()
            cursor = self._conn.execute(
                """
                INSERT INTO users(zhihu_user_id, name, headline, url, avatar_url, created_at, updated_at)
                VALUES ('', '匿名用户', '', '', '', ?, ?)
                """,
                (now, now),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def upsert_question(self, zhihu_question_id: str, title: str, url: str) -> int | None:
        if not zhihu_question_id and not url:
            return None
        now = now_iso()
        with self._lock:
            row = None
            if zhihu_question_id:
                row = self._conn.execute(
                    "SELECT id FROM questions WHERE zhihu_question_id = ?",
                    (zhihu_question_id,),
                ).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO questions(zhihu_question_id, title, url, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (zhihu_question_id, title, url, now, now),
                )
                self._conn.commit()
                return int(cursor.lastrowid)
            if title:
                self._conn.execute(
                    "UPDATE questions SET title = ?, url = ?, updated_at = ? WHERE id = ?",
                    (title, url, now, row["id"]),
                )
            else:
                self._conn.execute(
                    "UPDATE questions SET url = ?, updated_at = ? WHERE id = ?",
                    (url, now, row["id"]),
                )
            self._conn.commit()
            return int(row["id"])

    def save_content(
        self,
        record: ContentRecord,
        *,
        markdown: str,
        html: str,
        index_scope: str = "author",
    ) -> tuple[str, int]:
        """返回 (inserted/updated/skipped, 本地 id)。可靠的知乎 ID 不会因为正文相同被合并。"""
        content_id = record.zhihu_content_id if _is_reliable_id(record.zhihu_content_id) else _fallback_identity(record)
        now = now_iso()
        created = unix_to_iso(record.created_time)
        updated = unix_to_iso(record.updated_time)
        author_id = self.upsert_user(record.author)
        question_id = self.upsert_question(record.question_id, record.question_title, record.question_url)
        with self._lock:
            existing = self._find_existing(content_id, record)
            if existing is None:
                export_index = self._next_export_index(
                    author_id,
                    record.content_type,
                    question_id,
                    index_scope,
                )
                cursor = self._conn.execute(
                    """
                    INSERT INTO contents(
                        zhihu_content_id, content_type, author_id, question_id, title, url,
                        created_time, updated_time, vote_count, comment_count, html_content,
                        markdown_content, content_hash, download_time, last_checked_time,
                        author_name, excerpt, export_index
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        content_id,
                        record.content_type,
                        author_id,
                        question_id,
                        record.question_title or record.title,
                        record.url,
                        created,
                        updated,
                        record.vote_count,
                        record.comment_count,
                        html,
                        markdown,
                        record.content_hash,
                        now,
                        now,
                        record.author.name,
                        record.excerpt,
                        export_index,
                    ),
                )
                if record.content_type == "answer" and _is_reliable_id(content_id):
                    self._conn.execute(
                        "UPDATE collection_members SET content_id = ? WHERE answer_id = ?",
                        (int(cursor.lastrowid), content_id),
                    )
                self._conn.commit()
                return "inserted", int(cursor.lastrowid)
            upgraded_id = content_id if _is_reliable_id(content_id) else existing["zhihu_content_id"]
            incoming_html = (record.html_content or "").strip()
            incomplete = bool(getattr(record, "content_incomplete", False)) or not incoming_html
            if incomplete:
                self._conn.execute(
                    "UPDATE contents SET zhihu_content_id = ?, last_checked_time = ? WHERE id = ?",
                    (upgraded_id, now, existing["id"]),
                )
                if record.content_type == "answer" and _is_reliable_id(upgraded_id):
                    self._conn.execute(
                        "UPDATE collection_members SET content_id = ? WHERE answer_id = ?",
                        (int(existing["id"]), upgraded_id),
                    )
                self._conn.commit()
                return "skipped", int(existing["id"])
            same_hash = (existing["content_hash"] or "") == (record.content_hash or "")
            # 缺失的字段保留已有值；只有响应明确给出的新值才覆盖。
            title = (record.question_title or record.title) or existing["title"]
            author_name = record.author.name or existing["author_name"]
            url = record.url or existing["url"]
            stored_author_id = author_id if author_id is not None else existing["author_id"]
            stored_question_id = question_id if question_id is not None else existing["question_id"]
            excerpt = record.excerpt or existing["excerpt"]
            if same_hash:
                vote = existing["vote_count"]
                comment = existing["comment_count"]
                meta = False
                if record.vote_count is not None and record.vote_count != existing["vote_count"]:
                    vote = record.vote_count
                    meta = True
                if record.comment_count is not None and record.comment_count != existing["comment_count"]:
                    comment = record.comment_count
                    meta = True
                stored_updated = updated or existing["updated_time"]
                if updated and updated != (existing["updated_time"] or ""):
                    meta = True
                stored_created = created or existing["created_time"]
                # 正文没变但标题、作者快照或来源变了，也属于元数据更新，需要刷新内容库和导出指纹。
                for new_value, column in (
                    (title, "title"),
                    (author_name, "author_name"),
                    (url, "url"),
                    (stored_author_id, "author_id"),
                    (stored_question_id, "question_id"),
                    (stored_created, "created_time"),
                ):
                    if (new_value or None) != (existing[column] or None):
                        meta = True
                self._conn.execute(
                    """
                    UPDATE contents
                    SET zhihu_content_id = ?, vote_count = ?, comment_count = ?,
                        updated_time = ?, last_checked_time = ?, title = ?, author_name = ?,
                        url = ?, author_id = ?, question_id = ?, created_time = ?, excerpt = ?
                    WHERE id = ?
                    """,
                    (
                        upgraded_id, vote, comment, stored_updated, now, title, author_name,
                        url, stored_author_id, stored_question_id, stored_created, excerpt, existing["id"],
                    ),
                )
                if record.content_type == "answer" and _is_reliable_id(upgraded_id):
                    self._conn.execute(
                        "UPDATE collection_members SET content_id = ? WHERE answer_id = ?",
                        (int(existing["id"]), upgraded_id),
                    )
                self._conn.commit()
                return ("metadata" if meta else "skipped"), int(existing["id"])
            self._conn.execute(
                """
                UPDATE contents
                SET zhihu_content_id = ?, author_id = ?, question_id = ?, title = ?, url = ?, created_time = ?,
                    updated_time = ?, vote_count = ?, comment_count = ?, html_content = ?,
                    markdown_content = ?, content_hash = ?, download_time = ?, last_checked_time = ?,
                    author_name = ?, excerpt = ?
                WHERE id = ?
                """,
                (
                    upgraded_id,
                    stored_author_id,
                    stored_question_id,
                    title,
                    url,
                    created or existing["created_time"],
                    updated,
                    record.vote_count,
                    record.comment_count,
                    html,
                    markdown,
                    record.content_hash,
                    now,
                    now,
                    author_name,
                    record.excerpt,
                    existing["id"],
                ),
            )
            if record.content_type == "answer" and _is_reliable_id(upgraded_id):
                self._conn.execute(
                    "UPDATE collection_members SET content_id = ? WHERE answer_id = ?",
                    (int(existing["id"]), upgraded_id),
                )
            self._conn.commit()
            return "updated", int(existing["id"])

    def replace_images(self, content_local_id: int, images: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM images WHERE content_id = ?", (content_local_id,))
            for image in images:
                self._conn.execute(
                    """
                    INSERT INTO images(content_id, original_url, local_path, download_status, hash, sort_index)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        content_local_id,
                        image.get("original_url", ""),
                        image.get("local_path", ""),
                        image.get("download_status", "pending"),
                        image.get("hash", ""),
                        image.get("sort_index", 0),
                    ),
                )
            self._conn.commit()

    def known_versions(self, *, author_url: str = "", question_zhihu_id: str = "", content_type: str = "") -> dict[str, tuple[str, str]]:
        query = """
            SELECT c.zhihu_content_id, c.updated_time, c.content_hash
            FROM contents c
            LEFT JOIN users u ON u.id = c.author_id
            LEFT JOIN questions q ON q.id = c.question_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if content_type:
            query += " AND c.content_type = ?"
            params.append(content_type)
        if author_url:
            query += " AND u.url = ?"
            params.append(author_url)
        if question_zhihu_id:
            query += " AND q.zhihu_question_id = ?"
            params.append(question_zhihu_id)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return {
            row["zhihu_content_id"]: (row["updated_time"] or "", row["content_hash"] or "")
            for row in rows
            if row["zhihu_content_id"]
        }

    def count_contents(
        self,
        *,
        author_url: str = "",
        question_zhihu_id: str = "",
        content_type: str = "",
        zhihu_content_id: str = "",
    ) -> int:
        query = """
            SELECT COUNT(*) AS value
            FROM contents c
            LEFT JOIN users u ON u.id = c.author_id
            LEFT JOIN questions q ON q.id = c.question_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if content_type:
            query += " AND c.content_type = ?"
            params.append(content_type)
        if author_url:
            query += " AND u.url = ?"
            params.append(author_url)
        if question_zhihu_id:
            query += " AND q.zhihu_question_id = ?"
            params.append(question_zhihu_id)
        if zhihu_content_id:
            query += " AND c.zhihu_content_id = ?"
            params.append(zhihu_content_id)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return int(row["value"])

    def list_contents(
        self,
        *,
        author_url: str = "",
        question_zhihu_id: str = "",
        content_type: str = "",
        zhihu_content_id: str = "",
    ) -> list[sqlite3.Row]:
        query = """
            SELECT c.*, u.name AS user_name, u.url AS user_url, q.title AS question_title_db, q.url AS question_url_db
            FROM contents c
            LEFT JOIN users u ON u.id = c.author_id
            LEFT JOIN questions q ON q.id = c.question_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if content_type:
            query += " AND c.content_type = ?"
            params.append(content_type)
        if author_url:
            query += " AND u.url = ?"
            params.append(author_url)
        if question_zhihu_id:
            query += " AND q.zhihu_question_id = ?"
            params.append(question_zhihu_id)
        if zhihu_content_id:
            query += " AND c.zhihu_content_id = ?"
            params.append(zhihu_content_id)
        query += " ORDER BY COALESCE(c.export_index, c.id)"
        with self._lock:
            return list(self._conn.execute(query, params).fetchall())

    def get_content(self, content_local_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                """
                SELECT c.*, u.name AS user_name, u.url AS user_url, u.url AS author_url,
                       q.zhihu_question_id, q.title AS question_title_db, q.url AS question_url_db
                FROM contents c
                LEFT JOIN users u ON u.id = c.author_id
                LEFT JOIN questions q ON q.id = c.question_id
                WHERE c.id = ?
                """,
                (content_local_id,),
            ).fetchone()

    def list_images(self, content_local_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM images WHERE content_id = ? ORDER BY sort_index, id",
                    (content_local_id,),
                ).fetchall()
            )

    def create_task(self, *, task_type: str, target_url: str, target_id: str, output_dir: str, options: dict) -> int:
        now = now_iso()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO tasks(
                    task_type, target_url, target_id, status, total_found, total_completed,
                    created_at, updated_at, error_message, output_dir, options_json
                ) VALUES (?, ?, ?, 'running', 0, 0, ?, ?, '', ?, ?)
                """,
                (task_type, target_url, target_id, now, now, output_dir, json.dumps(options, ensure_ascii=False)),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def update_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {columns} WHERE id = ?",
                (*fields.values(), task_id),
            )
            self._conn.commit()

    def latest_task(self, target_url: str, task_type: str = "") -> sqlite3.Row | None:
        query = "SELECT * FROM tasks WHERE target_url = ?"
        params: list[Any] = [target_url]
        if task_type:
            query += " AND task_type = ?"
            params.append(task_type)
        query += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            return self._conn.execute(query, params).fetchone()

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()

    def list_tasks(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall())

    def delete_task(self, task_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()

    @serialized_export
    def record_export(
        self,
        content_local_id: int,
        *,
        export_format: str,
        output_path: str,
        content_hash: str,
        status: str,
        error_message: str = "",
        fingerprint: str = "",
        export_type: str = "separate",
        scope_json: str = "",
    ) -> None:
        now = now_iso()
        output_path = str(Path(output_path).resolve())
        key = target_key(output_path)
        file_size = None
        file_sha256 = None
        if status == "success":
            # 记录产物本身，之后才能识别空文件、损坏或外部修改，而不是只看路径存在。
            try:
                file_size, file_sha256 = file_digest(Path(output_path))
            except OSError:
                file_size, file_sha256 = None, None
        with self._lock:
            self._conn.execute("""UPDATE exports SET status='superseded', error_message='已被后续导出替代'
                WHERE target_key=? AND format=? AND NOT (content_id=? AND output_path=?)""",
                (key, export_format, content_local_id, output_path))
            self._conn.execute(
                f"""
                INSERT INTO exports(
                    content_id, format, output_path, content_hash, status, error_message, updated_at, fingerprint, export_type, scope_json, target_key, revision, type_explicit,
                    event_seq, file_size, file_sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, {_NEXT_EVENT_SEQ}, ?, ?)
                ON CONFLICT(content_id, format, output_path) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at,
                    fingerprint = excluded.fingerprint,
                    export_type = excluded.export_type, scope_json = excluded.scope_json,
                    target_key=excluded.target_key, revision=exports.revision+1, type_explicit=1,
                    event_seq = excluded.event_seq, file_size = excluded.file_size, file_sha256 = excluded.file_sha256
                """,
                (content_local_id, export_format, output_path, content_hash, status, error_message, now, fingerprint, export_type, scope_json, key, file_size, file_sha256),
            )
            self._conn.commit()

    def export_is_current(self, content_local_id: int, export_format: str, output_path: str, fingerprint: str) -> bool:
        """指纹相同且产物本身仍是当初写出的有效文件，才算可以复用。"""
        output_path = str(Path(output_path).resolve())
        if not fingerprint:
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT status, fingerprint, file_size, file_sha256 FROM exports
                WHERE content_id = ? AND format = ? AND output_path = ?
                """,
                (content_local_id, export_format, output_path),
            ).fetchone()
        if row is None or row["status"] != "success":
            return False
        if (row["fingerprint"] or "") != fingerprint:
            return False
        return self.recorded_artifact_problem(row, Path(output_path), export_format) == ""

    @staticmethod
    def recorded_artifact_problem(row, path: Path, export_format: str) -> str:
        """与登记的大小、摘要对照；旧记录没有摘要时至少检查文件结构。"""
        problem = artifact_problem(path, export_format)
        if problem:
            return problem
        size = row["file_size"] if "file_size" in row.keys() else None
        digest = row["file_sha256"] if "file_sha256" in row.keys() else None
        if digest:
            try:
                current_size, current_digest = file_digest(path)
            except OSError as exc:
                return f"无法读取文件：{exc}"
            if (size is not None and int(size) != current_size) or current_digest != digest:
                return "文件在导出后被修改或损坏"
        return ""

    def successful_exports(self, content_local_id: int | None = None, *, export_type: str = "") -> list[sqlite3.Row]:
        query = "SELECT * FROM exports WHERE status = 'success'"
        params: list[Any] = []
        if content_local_id is not None:
            query += " AND content_id = ?"
            params.append(int(content_local_id))
        if export_type:
            query += " AND COALESCE(export_type, 'separate') = ?"
            params.append(export_type)
        query += " ORDER BY id"
        with self._lock:
            return list(self._conn.execute(query, params).fetchall())

    def remember_export_stem(self, content_local_id: int, directory: Path, stem: str) -> None:
        key = str(directory.resolve())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO content_paths(content_id, directory, stem)
                VALUES (?, ?, ?)
                ON CONFLICT(content_id, directory) DO UPDATE SET stem = excluded.stem
                """,
                (content_local_id, key, stem),
            )
            self._conn.commit()

    def export_stem(self, content_local_id: int, directory: Path) -> str:
        key = str(directory.resolve())
        with self._lock:
            row = self._conn.execute(
                "SELECT stem FROM content_paths WHERE content_id = ? AND directory = ?",
                (content_local_id, key),
            ).fetchone()
        return row["stem"] if row else ""

    def stems_owned_by_others(self, directory: Path, content_local_id: int) -> set[str]:
        key = str(directory.resolve())
        with self._lock:
            rows = self._conn.execute(
                "SELECT stem FROM content_paths WHERE directory = ? AND content_id != ?",
                (key, content_local_id),
            ).fetchall()
        return {row["stem"].lower() for row in rows if row["stem"]}

    def search_contents(
        self,
        *,
        keyword: str = "",
        author: str = "",
        content_type: str = "",
        collection_id: str = "",
        source_id: int | None = None,
        read_state: str = "",
        starred_only: bool = False,
        tag: str = "",
        published_from: str = "",
        published_to: str = "",
        collected_from: str = "",
        collected_to: str = "",
        sort: str = "id_desc",
        task_id: int | None = None,
        task_outcome: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[sqlite3.Row], int]:
        from zhihu_collector.database.catalog import fetch_source, source_filter_sql

        where = ["1 = 1"]
        params: list[Any] = []
        if keyword.strip():
            like = like_pattern(keyword.strip())
            where.append(
                "(c.title LIKE ? ESCAPE '\\' OR c.html_content LIKE ? ESCAPE '\\'"
                " OR c.markdown_content LIKE ? ESCAPE '\\' OR c.excerpt LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like, like])
        if author.strip():
            where.append("c.author_name LIKE ? ESCAPE '\\'")
            params.append(like_pattern(author.strip()))
        if content_type:
            where.append("c.content_type = ?")
            params.append(content_type)
        if collection_id:
            # 收藏夹成员只有回答；同 ID 的想法等其他类型不能混入。
            where.append(
                "c.content_type = 'answer' AND c.zhihu_content_id IN "
                "(SELECT answer_id FROM collection_members WHERE collection_id = ?)"
            )
            params.append(collection_id)
        if source_id:
            source = fetch_source(self, int(source_id))
            if source is None:
                where.append("1 = 0")
            else:
                clause, source_params = source_filter_sql(source)
                where.append(clause)
                params.extend(source_params)
        if read_state == "read":
            where.append("COALESCE((SELECT is_read FROM content_user_state WHERE content_id = c.id), 0) = 1")
        elif read_state == "unread":
            where.append("COALESCE((SELECT is_read FROM content_user_state WHERE content_id = c.id), 0) = 0")
        if starred_only:
            where.append("COALESCE((SELECT starred FROM content_user_state WHERE content_id = c.id), 0) = 1")
        if tag.strip():
            where.append(
                """c.id IN (
                    SELECT ct.content_id FROM content_tags ct
                    JOIN tags t ON t.id = ct.tag_id
                    WHERE t.name = ?
                )"""
            )
            params.append(tag.strip())
        if published_from:
            where.append("substr(COALESCE(c.created_time, ''), 1, 10) >= ?")
            params.append(published_from)
        if published_to:
            where.append("substr(COALESCE(c.created_time, ''), 1, 10) <= ?")
            params.append(published_to)
        if collected_from:
            where.append("substr(COALESCE(c.download_time, ''), 1, 10) >= ?")
            params.append(collected_from)
        if collected_to:
            where.append("substr(COALESCE(c.download_time, ''), 1, 10) <= ?")
            params.append(collected_to)
        if task_id:
            if task_outcome:
                if task_outcome in ("updated", "body_updated"):
                    where.append(
                        "c.id IN (SELECT content_id FROM task_content_results WHERE task_id = ? AND outcome IN ('updated', 'body_updated'))"
                    )
                    params.append(int(task_id))
                elif task_outcome in ("metadata", "metadata_updated"):
                    where.append(
                        "c.id IN (SELECT content_id FROM task_content_results WHERE task_id = ? AND outcome IN ('metadata', 'metadata_updated'))"
                    )
                    params.append(int(task_id))
                else:
                    where.append(
                        "c.id IN (SELECT content_id FROM task_content_results WHERE task_id = ? AND outcome = ?)"
                    )
                    params.extend([int(task_id), task_outcome])
            else:
                where.append("c.id IN (SELECT content_id FROM task_content_results WHERE task_id = ?)")
                params.append(int(task_id))
        clause = " AND ".join(where)
        order = _CONTENT_SORTS.get(sort, _CONTENT_SORTS["id_desc"])
        with self._lock:
            total = int(
                self._conn.execute(f"SELECT COUNT(*) AS value FROM contents c WHERE {clause}", params).fetchone()["value"]
            )
            rows = list(
                self._conn.execute(
                    f"""
                    SELECT c.id, c.title, c.author_name, c.content_type, c.updated_time, c.created_time,
                           c.url, c.zhihu_content_id, c.excerpt, c.vote_count, c.download_time, c.content_hash,
                           COALESCE((SELECT is_read FROM content_user_state WHERE content_id = c.id), 0) AS is_read,
                           COALESCE((SELECT starred FROM content_user_state WHERE content_id = c.id), 0) AS starred,
                           (
                               SELECT group_concat(t.name, '、')
                               FROM content_tags ct JOIN tags t ON t.id = ct.tag_id
                               WHERE ct.content_id = c.id
                           ) AS tag_names,
                           SUM(CASE WHEN e.status = 'success' THEN 1 ELSE 0 END) AS export_success,
                           SUM(CASE WHEN e.status = 'failed' THEN 1 ELSE 0 END) AS export_failed,
                           CASE WHEN EXISTS (
                               SELECT 1 FROM exports stale
                               WHERE stale.content_id = c.id AND stale.status = 'success'
                                 AND COALESCE(stale.content_hash, '') != ''
                                 AND stale.content_hash != c.content_hash
                                 AND COALESCE(stale.export_type, 'separate') != 'merged'
                           ) THEN 1 ELSE 0 END AS export_stale
                    FROM contents c
                    LEFT JOIN exports e ON e.content_id = c.id
                    WHERE {clause}
                    GROUP BY c.id
                    ORDER BY {order}
                    LIMIT ? OFFSET ?
                    """,
                    (*params, max(1, limit), max(0, offset)),
                ).fetchall()
            )
        return rows, total

    def list_failures(self) -> list[sqlite3.Row]:
        with self._lock:
            exports = list(
                self._conn.execute(
                    """
                    SELECT 'export' AS kind, e.id AS item_id, e.content_id,
                           COALESCE(c.title, '合并导出') AS title,
                           COALESCE(c.author_name, '') AS author_name,
                           CASE WHEN e.export_type = 'merged' THEN 'merged' ELSE COALESCE(c.content_type, 'answer') END AS content_type,
                           e.format AS detail, e.error_message AS reason, e.output_path AS target,
                           CASE WHEN e.type_explicit=1 OR e.scope_json<>'' THEN e.export_type ELSE 'legacy' END AS export_type, e.revision,
                           COALESCE(e.scope_json, '') AS scope_json
                    FROM exports e
                    LEFT JOIN contents c ON c.id = e.content_id
                    WHERE e.status = 'failed'
                    ORDER BY e.id DESC
                    """
                ).fetchall()
            )
            image_rows = list(
                self._conn.execute(
                    """
                    SELECT 'image' AS kind, i.id AS item_id, i.content_id, c.title, c.author_name,
                           c.content_type, i.original_url AS detail, i.download_status AS reason, i.local_path AS target,
                           'image' AS export_type, '' AS scope_json, i.download_status, i.local_path, i.hash
                    FROM images i
                    JOIN contents c ON c.id = i.content_id
                    ORDER BY i.id DESC
                    """
                ).fetchall()
            )
        # 不只相信数据库状态：标记已下载但文件缺失、为空或摘要不符的图片也要出现在失败项里。
        images = []
        for row in image_rows:
            if row["download_status"] != "downloaded":
                images.append(row)
                continue
            problem = image_problem({"download_status": "downloaded", "local_path": row["local_path"], "hash": row["hash"]})
            if problem:
                item = {key: row[key] for key in row.keys()}
                item["reason"] = problem
                images.append(_FailureItem(item))
        with self._lock:
            bodies = list(self._conn.execute("""SELECT 'body' AS kind, id AS item_id,
                NULL AS content_id, title, '' AS author_name, content_type, url AS detail,
                reason, url AS target, 'body' AS export_type, '' AS scope_json,
                zhihu_content_id, task_id, options_json FROM content_failures WHERE status='failed'
                ORDER BY id""").fetchall())
        return exports + images + bodies

    def earliest_open_body_failure_time(self, zhihu_ids: list[str]) -> str:
        times: list[str] = []
        with self._lock:
            for zhihu_id in zhihu_ids:
                row = self._conn.execute(
                    """
                    SELECT updated_at FROM content_failures
                    WHERE zhihu_content_id = ? AND status = 'failed' AND updated_at != ''
                    """,
                    (str(zhihu_id),),
                ).fetchone()
                if row and row["updated_at"]:
                    times.append(row["updated_at"])
        return min(times) if times else ""

    def earliest_open_body_failure_seq(self, zhihu_ids: list[str]) -> int:
        """与 earliest_open_body_failure_time 对应的事件序号；旧记录没有序号时返回 0。"""
        seqs: list[int] = []
        with self._lock:
            for zhihu_id in zhihu_ids:
                row = self._conn.execute(
                    """
                    SELECT event_seq FROM content_failures
                    WHERE zhihu_content_id = ? AND status = 'failed' AND event_seq IS NOT NULL
                    """,
                    (str(zhihu_id),),
                ).fetchone()
                if row and row["event_seq"]:
                    seqs.append(int(row["event_seq"]))
        return min(seqs) if seqs else 0

    def failure_seq_at(self, failed_at: str) -> int:
        """按失败时间找到对应的正文失败事件序号（同一秒有多条时取最早的）。"""
        if not failed_at:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(event_seq) AS value FROM content_failures WHERE updated_at = ? AND event_seq IS NOT NULL",
                (failed_at,),
            ).fetchone()
        return int(row["value"] or 0) if row else 0

    def latest_successful_export_for_target(self, output_path: str, export_format: str):
        key = target_key(output_path)
        with self._lock:
            return self._conn.execute(
                """
                SELECT updated_at, content_id, output_path, revision, event_seq
                FROM exports
                WHERE status = 'success' AND format = ? AND target_key = ?
                ORDER BY updated_at DESC, COALESCE(event_seq, 0) DESC, id DESC
                LIMIT 1
                """,
                (export_format, key),
            ).fetchone()

    def export_retry_is_current(self, item, *, status="failed"):
        with self._lock:
            row = self._conn.execute('SELECT status, revision FROM exports WHERE id=?', (item['item_id'],)).fetchone()
            return bool(row and row['status']==status and row['revision']==item['revision'])

    def record_content_failure(self, record, task_id, options, reason):
        with self._lock:
            self._conn.execute(f"""INSERT INTO content_failures(content_type,zhihu_content_id,url,title,task_id,options_json,reason,status,updated_at,event_seq)
                VALUES(?,?,?,?,?,?,?,'failed',?,{_NEXT_EVENT_SEQ}) ON CONFLICT(content_type,zhihu_content_id) DO UPDATE SET
                url=excluded.url,title=excluded.title,task_id=excluded.task_id,options_json=excluded.options_json,
                reason=excluded.reason,status='failed',updated_at=excluded.updated_at,event_seq=excluded.event_seq""",
                (record.content_type, record.zhihu_content_id, record.url, record.title or record.question_title,
                 task_id, json.dumps(options,ensure_ascii=False), str(reason), now_iso()))
            self._conn.commit()

    def resolve_content_failure(self, content_type, content_id):
        with self._lock:
            self._conn.execute("UPDATE content_failures SET status='resolved',updated_at=? WHERE content_type=? AND zhihu_content_id=?",
                               (now_iso(),content_type,content_id))
            self._conn.commit()

    def export_formats(self, content_local_id: int) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT format FROM exports WHERE content_id = ?",
                (content_local_id,),
            ).fetchall()
        return {row["format"] for row in rows if row["format"]}

    def successful_export_paths(self, content_local_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT output_path FROM exports
                WHERE content_id = ? AND status = 'success' AND output_path != ''
                ORDER BY id
                """,
                (content_local_id,),
            ).fetchall()
        return [row["output_path"] for row in rows]

    def latest_export_path(self, content_local_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT output_path FROM exports
                WHERE content_id = ? AND output_path != ''
                ORDER BY CASE status WHEN 'success' THEN 0 ELSE 1 END, id DESC
                LIMIT 1
                """,
                (content_local_id,),
            ).fetchone()
        return row["output_path"] if row else ""

    def preferred_markdown_path(self, content_local_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT output_path FROM exports
                WHERE content_id = ? AND format = 'markdown' AND status = 'success'
                ORDER BY id DESC LIMIT 1
                """,
                (content_local_id,),
            ).fetchone()
        return row["output_path"] if row else ""

    def export_paths_for_others(self, directory: Path, content_local_id: int) -> set[str]:
        target = directory.resolve()
        with self._lock:
            rows = self._conn.execute(
                "SELECT content_id, output_path FROM exports WHERE status = 'success'",
            ).fetchall()
        taken: set[str] = set()
        for row in rows:
            if int(row["content_id"]) == content_local_id or not row["output_path"]:
                continue
            path = Path(row["output_path"])
            if path.parent.resolve() == target:
                taken.add(str(path.resolve()).lower())
        return taken

    def save_collection(self, collection_id: str, title: str, url: str, updated_time: str = "") -> int:
        now = now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO collections (zhihu_collection_id, title, url, updated_time, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(zhihu_collection_id) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    updated_time = CASE WHEN excluded.updated_time <> '' THEN excluded.updated_time ELSE collections.updated_time END,
                    updated_at = excluded.updated_at
                """,
                (str(collection_id), title, url, updated_time, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM collections WHERE zhihu_collection_id = ?",
                (str(collection_id),),
            ).fetchone()
            return int(row["id"]) if row else 0

    def get_collection(self, collection_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM collections WHERE zhihu_collection_id = ?",
                (str(collection_id),),
            ).fetchone()

    def list_collections(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM collections ORDER BY id DESC").fetchall())

    def save_collection_members(self, collection_id: str, members: list[dict]) -> None:
        now = now_iso()
        with self._lock:
            for member in members:
                answer_id = str(member.get("answer_id") or "")
                if not answer_id:
                    continue
                sort_order = int(member.get("sort_order") or 0)
                status = str(member.get("item_status") or "discovered")
                content_row = self._conn.execute(
                    "SELECT id FROM contents WHERE content_type = 'answer' AND zhihu_content_id = ?",
                    (answer_id,),
                ).fetchone()
                content_id = int(content_row["id"]) if content_row else member.get("content_id")

                self._conn.execute(
                    """
                    INSERT INTO collection_members (collection_id, answer_id, content_id, sort_order, item_status, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(collection_id, answer_id) DO UPDATE SET
                        content_id = COALESCE(excluded.content_id, collection_members.content_id),
                        sort_order = excluded.sort_order,
                        item_status = excluded.item_status
                    """,
                    (str(collection_id), answer_id, content_id, sort_order, status, now),
                )
            self._conn.commit()

    def get_collection_members(self, collection_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    """
                    SELECT m.*, c.title, c.author_name, c.url, c.html_content, c.markdown_content, c.content_hash
                    FROM collection_members m
                    LEFT JOIN contents c ON c.id = m.content_id
                    WHERE m.collection_id = ?
                    ORDER BY m.sort_order ASC, m.id ASC
                    """,
                    (str(collection_id),),
                ).fetchall()
            )

    def create_collection_snapshot(
        self,
        task_id: int | None,
        collection_id: str,
        member_ids: list[str],
        total_declared: int = 0,
        discovered_answers: int = 0,
        non_answer_count: int = 0,
        duplicate_count: int = 0,
        integrity_status: str = "unknown",
        finish_reason: str = "",
    ) -> int:
        now = now_iso()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO collection_snapshots (
                    task_id, collection_id, member_ids_json, total_declared,
                    discovered_answers, non_answer_count, duplicate_count,
                    integrity_status, finish_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    str(collection_id),
                    json.dumps(member_ids, ensure_ascii=False),
                    total_declared,
                    discovered_answers,
                    non_answer_count,
                    duplicate_count,
                    integrity_status,
                    finish_reason,
                    now,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def update_collection_snapshot(self, snapshot_id: int, **fields: Any) -> None:
        if not fields:
            return
        if fields.pop("finished", False):
            fields["finished_at"] = now_iso()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE collection_snapshots SET {columns} WHERE id = ?",
                (*fields.values(), snapshot_id),
            )
            self._conn.commit()

    def get_collection_snapshot(self, task_id: int | None = None, collection_id: str | None = None) -> sqlite3.Row | None:
        with self._lock:
            if task_id:
                return self._conn.execute(
                    "SELECT * FROM collection_snapshots WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
            if collection_id:
                return self._conn.execute(
                    "SELECT * FROM collection_snapshots WHERE collection_id = ? ORDER BY id DESC LIMIT 1",
                    (str(collection_id),),
                ).fetchone()
            return None

    def _find_existing(self, content_id: str, record: ContentRecord) -> sqlite3.Row | None:
        if _is_reliable_id(record.zhihu_content_id):
            row = self._conn.execute(
                "SELECT * FROM contents WHERE content_type = ? AND zhihu_content_id = ?",
                (record.content_type, record.zhihu_content_id),
            ).fetchone()
            if row is not None:
                return row
            if record.url:
                by_url = self._conn.execute(
                    "SELECT * FROM contents WHERE content_type = ? AND url = ?",
                    (record.content_type, record.url),
                ).fetchone()
                if by_url is not None and not _is_reliable_id(by_url["zhihu_content_id"]):
                    return by_url
            return None
        if record.url:
            return self._conn.execute(
                "SELECT * FROM contents WHERE content_type = ? AND url = ?",
                (record.content_type, record.url),
            ).fetchone()
        if record.content_hash:
            rows = self._conn.execute(
                "SELECT * FROM contents WHERE content_type = ? AND content_hash = ?",
                (record.content_type, record.content_hash),
            ).fetchall()
            for row in rows:
                if not _is_reliable_id(row["zhihu_content_id"]):
                    return row
        return None

    def _next_export_index(
        self,
        author_id: int | None,
        content_type: str,
        question_id: int | None,
        index_scope: str = "author",
    ) -> int:
        if index_scope == "question" and question_id:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(export_index), 0) AS value FROM contents WHERE question_id = ? AND content_type = ?",
                (question_id, content_type),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(export_index), 0) AS value FROM contents WHERE author_id IS ? AND content_type = ?",
                (author_id, content_type),
            ).fetchone()
        return int(row["value"]) + 1


class _FailureItem(dict):
    """与 sqlite3.Row 一样可按列名读取、调用 keys()，用于经过文件健康检查后补充原因的失败项。"""


def _is_reliable_id(value: str) -> bool:
    text = (value or "").strip()
    return bool(text) and not text.startswith(("url:", "hash:"))


def _fallback_identity(record: ContentRecord) -> str:
    if record.url:
        return f"url:{record.url}"
    return f"hash:{record.content_hash}"
