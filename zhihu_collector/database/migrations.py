"""建表。用户要求的字段都在，另外补了断点续传和导出需要的列。"""

from __future__ import annotations

SCHEMA_VERSION = 7

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            zhihu_user_id TEXT,
            name TEXT,
            headline TEXT,
            url TEXT,
            avatar_url TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY,
            zhihu_question_id TEXT,
            title TEXT,
            url TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS contents (
            id INTEGER PRIMARY KEY,
            zhihu_content_id TEXT,
            content_type TEXT NOT NULL,
            author_id INTEGER,
            question_id INTEGER,
            title TEXT,
            url TEXT,
            created_time TEXT,
            updated_time TEXT,
            vote_count INTEGER,
            comment_count INTEGER,
            html_content TEXT,
            markdown_content TEXT,
            content_hash TEXT,
            download_time TEXT,
            last_checked_time TEXT,
            author_name TEXT,
            excerpt TEXT,
            export_index INTEGER,
            FOREIGN KEY(author_id) REFERENCES users(id),
            FOREIGN KEY(question_id) REFERENCES questions(id)
        );

        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY,
            content_id INTEGER NOT NULL,
            original_url TEXT,
            local_path TEXT,
            download_status TEXT,
            hash TEXT,
            sort_index INTEGER,
            FOREIGN KEY(content_id) REFERENCES contents(id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY,
            task_type TEXT NOT NULL,
            target_url TEXT,
            target_id TEXT,
            status TEXT,
            total_found INTEGER DEFAULT 0,
            total_completed INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            error_message TEXT,
            output_dir TEXT,
            options_json TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_zhihu_id
            ON questions(zhihu_question_id) WHERE zhihu_question_id <> '';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_contents_identity
            ON contents(content_type, zhihu_content_id);
        CREATE INDEX IF NOT EXISTS idx_contents_url ON contents(url);
        CREATE INDEX IF NOT EXISTS idx_contents_hash ON contents(content_hash);
        CREATE INDEX IF NOT EXISTS idx_images_content ON images(content_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_target ON tasks(target_url, task_type);
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS exports (
            id INTEGER PRIMARY KEY,
            content_id INTEGER NOT NULL,
            format TEXT NOT NULL,
            output_path TEXT NOT NULL,
            content_hash TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            updated_at TEXT,
            UNIQUE(content_id, format, output_path),
            FOREIGN KEY(content_id) REFERENCES contents(id)
        );
        CREATE INDEX IF NOT EXISTS idx_exports_content ON exports(content_id);
        """,
    ),
    (
        3,
        """
        ALTER TABLE exports ADD COLUMN fingerprint TEXT;

        CREATE TABLE IF NOT EXISTS content_paths (
            id INTEGER PRIMARY KEY,
            content_id INTEGER NOT NULL,
            directory TEXT NOT NULL,
            stem TEXT NOT NULL,
            UNIQUE(content_id, directory)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_content_paths_location
            ON content_paths(directory, stem);
        """,
    ),
    (
        4,
        """
        ALTER TABLE exports ADD COLUMN export_type TEXT DEFAULT 'separate';
        ALTER TABLE exports ADD COLUMN scope_json TEXT DEFAULT '';
        """,
    ),
    (5, """
        ALTER TABLE exports ADD COLUMN target_key TEXT DEFAULT '';
        ALTER TABLE exports ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE exports ADD COLUMN type_explicit INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX idx_exports_target ON exports(target_key, format);
        CREATE TABLE content_failures (
            id INTEGER PRIMARY KEY, content_type TEXT NOT NULL, zhihu_content_id TEXT NOT NULL,
            url TEXT NOT NULL, title TEXT, task_id INTEGER, options_json TEXT NOT NULL,
            reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'failed', updated_at TEXT,
            UNIQUE(content_type, zhihu_content_id)
        );
    """),
    (
        6,
        """
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY,
            zhihu_collection_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            updated_time TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS collection_members (
            id INTEGER PRIMARY KEY,
            collection_id TEXT NOT NULL,
            answer_id TEXT NOT NULL,
            content_id INTEGER,
            sort_order INTEGER NOT NULL DEFAULT 0,
            item_status TEXT NOT NULL DEFAULT 'discovered',
            discovered_at TEXT NOT NULL,
            UNIQUE(collection_id, answer_id),
            FOREIGN KEY(content_id) REFERENCES contents(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_coll_members_coll ON collection_members(collection_id);
        CREATE INDEX IF NOT EXISTS idx_coll_members_ans ON collection_members(answer_id);

        CREATE TABLE IF NOT EXISTS collection_snapshots (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            collection_id TEXT NOT NULL,
            member_ids_json TEXT NOT NULL DEFAULT '[]',
            total_declared INTEGER DEFAULT 0,
            discovered_answers INTEGER DEFAULT 0,
            non_answer_count INTEGER DEFAULT 0,
            duplicate_count INTEGER DEFAULT 0,
            integrity_status TEXT NOT NULL DEFAULT 'unknown',
            finish_reason TEXT DEFAULT '',
            created_at TEXT,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_coll_snapshots_task ON collection_snapshots(task_id);
        CREATE INDEX IF NOT EXISTS idx_coll_snapshots_coll ON collection_snapshots(collection_id);
        """,
    ),
    (
        7,
        """
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            target_kind TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            collect_mode TEXT NOT NULL,
            preset_id TEXT NOT NULL DEFAULT '',
            preset_snapshot_json TEXT NOT NULL DEFAULT '{}',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            last_attempt_at TEXT,
            last_success_at TEXT,
            last_status TEXT NOT NULL DEFAULT '',
            last_result_json TEXT NOT NULL DEFAULT '{}',
            last_task_id INTEGER,
            UNIQUE(identity_key, collect_mode)
        );
        CREATE INDEX IF NOT EXISTS idx_sources_sort ON sources(sort_order, id);

        CREATE TABLE IF NOT EXISTS queue_items (
            id INTEGER PRIMARY KEY,
            batch_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            status TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            request_json TEXT NOT NULL,
            source_id INTEGER,
            task_id INTEGER,
            error_message TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_queue_status_pos ON queue_items(status, position, id);
        CREATE INDEX IF NOT EXISTS idx_queue_dedupe ON queue_items(dedupe_key, status);

        CREATE TABLE IF NOT EXISTS content_user_state (
            content_id INTEGER PRIMARY KEY,
            is_read INTEGER NOT NULL DEFAULT 0,
            starred INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            FOREIGN KEY(content_id) REFERENCES contents(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS content_tags (
            content_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY(content_id, tag_id),
            FOREIGN KEY(content_id) REFERENCES contents(id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_content_tags_tag ON content_tags(tag_id);

        CREATE TABLE IF NOT EXISTS task_content_results (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            content_id INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            UNIQUE(task_id, content_id)
        );
        CREATE INDEX IF NOT EXISTS idx_task_results_task ON task_content_results(task_id, outcome);

        CREATE INDEX IF NOT EXISTS idx_contents_created ON contents(created_time);
        CREATE INDEX IF NOT EXISTS idx_contents_checked ON contents(last_checked_time);
        CREATE INDEX IF NOT EXISTS idx_contents_votes ON contents(vote_count);
        """,
    ),
]
