from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Classification,
    CostSnapshot,
    ModelUsage,
    NewsOrigin,
    NotificationPayload,
    NotificationState,
    Post,
    PostState,
    QueryCursor,
    isoformat_z,
    parse_utc_datetime,
    utc_now,
)
from .origin import original_report_fingerprint


@dataclass(frozen=True, slots=True)
class StoredPost:
    post: Post
    source_key: str
    state: PostState
    classification_attempts: int


@dataclass(frozen=True, slots=True)
class StoredNotification:
    post_id: str
    bark_id: str
    title: str
    body: str
    url: str
    group_name: str
    level: str
    sound: str
    attempt_count: int

    def payload(self) -> NotificationPayload:
        return NotificationPayload(
            post_id=self.post_id,
            bark_id=self.bark_id,
            title=self.title,
            body=self.body,
            url=self.url,
            group=self.group_name,
            level=self.level,
            sound=self.sound,
        )


class StateStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level="IMMEDIATE",
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def _migrate(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS query_cursors (
            query_key TEXT PRIMARY KEY,
            query_fingerprint TEXT NOT NULL,
            since_id TEXT,
            last_success_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL DEFAULT '',
            author_id TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            referenced_types_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            state TEXT NOT NULL,
            classification_json TEXT,
            origin_fingerprint TEXT,
            news_origin TEXT,
            classification_attempts INTEGER NOT NULL DEFAULT 0,
            next_classification_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS posts_pending_idx
            ON posts(state, next_classification_at, created_at);

        CREATE TABLE IF NOT EXISTS notifications (
            post_id TEXT PRIMARY KEY REFERENCES posts(post_id),
            bark_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            url TEXT NOT NULL,
            group_name TEXT NOT NULL,
            level TEXT NOT NULL,
            sound TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sent_at TEXT
        );

        CREATE INDEX IF NOT EXISTS notifications_due_idx
            ON notifications(status, next_attempt_at);

        CREATE TABLE IF NOT EXISTS report_origins (
            origin_fingerprint TEXT PRIMARY KEY,
            first_post_id TEXT NOT NULL REFERENCES posts(post_id),
            first_accepted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS x_resource_daily (
            utc_day TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY (utc_day, resource_type, resource_id)
        );

        CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            status TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 1,
            units INTEGER NOT NULL DEFAULT 0,
            raw_units INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
            prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd TEXT NOT NULL DEFAULT '0'
        );

        CREATE INDEX IF NOT EXISTS api_usage_provider_time_idx
            ON api_usage(provider, occurred_at);

        CREATE TABLE IF NOT EXISTS health_flags (
            key TEXT PRIMARY KEY,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            active INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
        with self._lock:
            self._connection.executescript(schema)
            previous_version_row = self._connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            previous_version = (
                str(previous_version_row["value"])
                if previous_version_row is not None
                else None
            )
            post_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(posts)").fetchall()
            }
            if "origin_fingerprint" not in post_columns:
                self._connection.execute(
                    "ALTER TABLE posts ADD COLUMN origin_fingerprint TEXT"
                )
            if "news_origin" not in post_columns:
                self._connection.execute("ALTER TABLE posts ADD COLUMN news_origin TEXT")
            if previous_version != "2":
                accepted_posts = self._connection.execute(
                    """
                    SELECT p.*
                    FROM notifications AS notification
                    JOIN posts AS p ON p.post_id = notification.post_id
                    ORDER BY notification.created_at, notification.post_id
                    """
                ).fetchall()
                for row in accepted_posts:
                    origin = original_report_fingerprint(self._stored_post(row).post)
                    if origin is not None:
                        self._connection.execute(
                            """
                            INSERT OR IGNORE INTO report_origins(
                                origin_fingerprint, first_post_id, first_accepted_at
                            ) VALUES (?, ?, ?)
                            """,
                            (
                                origin.value,
                                row["post_id"],
                                row["updated_at"],
                            ),
                        )
            self._connection.execute(
                """
                INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO schema_meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def run_maintenance(self, usage_retention_days: int) -> bool:
        now = utc_now()
        last_raw = self.get_meta("maintenance_last_at")
        if last_raw and now - parse_utc_datetime(last_raw) < timedelta(hours=24):
            return False
        usage_cutoff = isoformat_z(now - timedelta(days=usage_retention_days))
        resource_day_cutoff = (
            now.astimezone(timezone.utc).date() - timedelta(days=2)
        ).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM api_usage WHERE occurred_at < ?",
                (usage_cutoff,),
            )
            self._connection.execute(
                "DELETE FROM x_resource_daily WHERE utc_day < ?",
                (resource_day_cutoff,),
            )
            self._connection.execute(
                """
                INSERT INTO schema_meta(key, value) VALUES ('maintenance_last_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (isoformat_z(now),),
            )
            self._connection.execute("PRAGMA optimize")
        return True

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_cursor(self, query_key: str, fingerprint: str) -> QueryCursor:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT query_key, query_fingerprint, since_id, last_success_at
                FROM query_cursors
                WHERE query_key = ?
                """,
                (query_key,),
            ).fetchone()
        if row is None or row["query_fingerprint"] != fingerprint:
            return QueryCursor(query_key, fingerprint, None, None)
        return QueryCursor(
            query_key=row["query_key"],
            query_fingerprint=row["query_fingerprint"],
            since_id=row["since_id"],
            last_success_at=(
                parse_utc_datetime(row["last_success_at"]) if row["last_success_at"] else None
            ),
        )

    def commit_cursor(
        self,
        query_key: str,
        fingerprint: str,
        newest_id: str | None,
        successful_at: datetime,
    ) -> None:
        now_text = isoformat_z(successful_at)
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT since_id, query_fingerprint FROM query_cursors WHERE query_key = ?",
                (query_key,),
            ).fetchone()
            retained_id = newest_id
            if (
                retained_id is None
                and existing is not None
                and existing["query_fingerprint"] == fingerprint
            ):
                retained_id = existing["since_id"]
            self._connection.execute(
                """
                INSERT INTO query_cursors(
                    query_key, query_fingerprint, since_id, last_success_at, last_error, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(query_key) DO UPDATE SET
                    query_fingerprint = excluded.query_fingerprint,
                    since_id = excluded.since_id,
                    last_success_at = excluded.last_success_at,
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (query_key, fingerprint, retained_id, now_text, now_text),
            )

    def mark_cursor_error(self, query_key: str, fingerprint: str, error_code: str) -> None:
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO query_cursors(
                    query_key, query_fingerprint, since_id, last_success_at, last_error, updated_at
                ) VALUES (?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(query_key) DO UPDATE SET
                    since_id = CASE
                        WHEN query_cursors.query_fingerprint = excluded.query_fingerprint
                        THEN query_cursors.since_id
                        ELSE NULL
                    END,
                    last_success_at = CASE
                        WHEN query_cursors.query_fingerprint = excluded.query_fingerprint
                        THEN query_cursors.last_success_at
                        ELSE NULL
                    END,
                    query_fingerprint = excluded.query_fingerprint,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (query_key, fingerprint, error_code[:200], now_text),
            )

    def store_posts(self, posts: Iterable[tuple[Post, str]]) -> int:
        now_text = isoformat_z(utc_now())
        inserted = 0
        with self._lock, self._connection:
            for post, source_key in posts:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO posts(
                        post_id, source_key, author_id, text, created_at,
                        referenced_types_json, raw_json, fetched_at, state, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        post.id,
                        source_key,
                        post.author_id,
                        post.text,
                        isoformat_z(post.created_at),
                        json.dumps(post.referenced_types, separators=(",", ":")),
                        post.to_record_json(),
                        now_text,
                        PostState.PENDING.value,
                        now_text,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def pending_posts(self, now: datetime | None = None, limit: int = 100) -> list[StoredPost]:
        current = isoformat_z(now or utc_now())
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT *
                FROM posts
                WHERE state IN (?, ?)
                  AND (next_classification_at IS NULL OR next_classification_at <= ?)
                ORDER BY created_at ASC, post_id ASC
                LIMIT ?
                """,
                (
                    PostState.PENDING.value,
                    PostState.CLASSIFICATION_RETRY.value,
                    current,
                    limit,
                ),
            ).fetchall()
        return [self._stored_post(row) for row in rows]

    def _stored_post(self, row: sqlite3.Row) -> StoredPost:
        raw = json.loads(row["raw_json"])
        post = Post(
            id=row["post_id"],
            author_id=row["author_id"],
            text=row["text"],
            created_at=parse_utc_datetime(row["created_at"]),
            referenced_types=tuple(json.loads(row["referenced_types_json"])),
            raw=raw,
        )
        return StoredPost(
            post=post,
            source_key=row["source_key"],
            state=PostState(row["state"]),
            classification_attempts=row["classification_attempts"],
        )

    def stored_post(self, post_id: str) -> StoredPost | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()
        return self._stored_post(row) if row is not None else None

    def has_notified_origin(self, origin_fingerprint: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1
                FROM report_origins
                WHERE origin_fingerprint = ?
                """,
                (origin_fingerprint,),
            ).fetchone()
        return row is not None

    def notified_origin_post(self, origin_fingerprint: str) -> StoredPost | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT p.*
                FROM report_origins AS origin
                JOIN posts AS p ON p.post_id = origin.first_post_id
                WHERE origin.origin_fingerprint = ?
                """,
                (origin_fingerprint,),
            ).fetchone()
        return self._stored_post(row) if row is not None else None

    def first_notification_post_id(self, post_ids: Iterable[str]) -> str | None:
        identifiers = tuple(dict.fromkeys(post_ids))
        if not identifiers:
            return None
        placeholders = ",".join("?" for _ in identifiers)
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT post_id
                FROM notifications
                WHERE post_id IN ({placeholders})
                ORDER BY created_at ASC, post_id ASC
                LIMIT 1
                """,
                identifiers,
            ).fetchone()
        return str(row["post_id"]) if row is not None else None

    def mark_duplicate_edited_post(
        self, post_id: str, previous_notification_post_id: str
    ) -> None:
        self._update_post(
            post_id,
            state=PostState.FILTERED.value,
            origin_fingerprint=f"edit:{previous_notification_post_id}",
            next_classification_at=None,
            last_error="duplicate_edited_post",
        )

    def assign_source(self, post_id: str, source_key: str) -> None:
        self._update_post(post_id, source_key=source_key)

    def increment_classification_attempt(self, post_id: str) -> int:
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE posts
                SET classification_attempts = classification_attempts + 1,
                    updated_at = ?
                WHERE post_id = ?
                """,
                (now_text, post_id),
            )
            row = self._connection.execute(
                "SELECT classification_attempts FROM posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()
        if row is None:
            raise KeyError(post_id)
        return int(row["classification_attempts"])

    def mark_post_terminal(
        self, post_id: str, state: PostState, error_code: str | None = None
    ) -> None:
        if state in {
            PostState.PENDING,
            PostState.CLASSIFICATION_RETRY,
            PostState.NOTIFICATION_PENDING,
        }:
            raise ValueError("terminal state required")
        self._update_post(
            post_id,
            state=state.value,
            last_error=error_code[:200] if error_code else None,
            next_classification_at=None,
        )

    def save_classification(
        self,
        post_id: str,
        result: Classification,
        origin_fingerprint: str | None = None,
    ) -> None:
        if result.eligible:
            raise ValueError("eligible classifications must be saved with their notification")
        self._update_post(
            post_id,
            state=PostState.FILTERED.value,
            classification_json=result.to_json(),
            origin_fingerprint=origin_fingerprint,
            news_origin=result.news_origin.value,
            last_error=None,
            next_classification_at=None,
        )

    def save_classification_and_enqueue(
        self,
        post_id: str,
        result: Classification,
        payload: NotificationPayload,
        dry_run: bool,
        origin_fingerprint: str | None = None,
    ) -> bool:
        if not result.eligible:
            raise ValueError("an eligible classification is required")
        if payload.post_id != post_id:
            raise ValueError("notification Post ID mismatch")
        now_text = isoformat_z(utc_now())
        notification_status = (
            NotificationState.DRY_RUN if dry_run else NotificationState.PENDING
        )
        post_state = PostState.DRY_RUN if dry_run else PostState.NOTIFICATION_PENDING
        with self._lock, self._connection:
            if (
                origin_fingerprint
                and result.news_origin is NewsOrigin.FIRST_HAND_REPORT
            ):
                existing_origin = self._connection.execute(
                    """
                    SELECT first_post_id
                    FROM report_origins
                    WHERE origin_fingerprint = ?
                    """,
                    (origin_fingerprint,),
                ).fetchone()
                if (
                    existing_origin is not None
                    and existing_origin["first_post_id"] != post_id
                ):
                    self._connection.execute(
                        """
                        UPDATE posts
                        SET state = ?, classification_json = ?,
                            origin_fingerprint = ?, news_origin = ?,
                            next_classification_at = NULL, last_error = ?,
                            updated_at = ?
                        WHERE post_id = ?
                        """,
                        (
                            PostState.FILTERED.value,
                            result.to_json(),
                            origin_fingerprint,
                            result.news_origin.value,
                            "duplicate_original_report",
                            now_text,
                            post_id,
                        ),
                    )
                    return False
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO notifications(
                    post_id, bark_id, status, title, body, url, group_name, level, sound,
                    next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.post_id,
                    payload.bark_id,
                    notification_status.value,
                    payload.title,
                    payload.body,
                    payload.url,
                    payload.group,
                    payload.level,
                    payload.sound,
                    now_text if not dry_run else None,
                    now_text,
                    now_text,
                ),
            )
            if cursor.rowcount and origin_fingerprint:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO report_origins(
                        origin_fingerprint, first_post_id, first_accepted_at
                    ) VALUES (?, ?, ?)
                    """,
                    (origin_fingerprint, post_id, now_text),
                )
            self._connection.execute(
                """
                UPDATE posts
                SET state = ?, classification_json = ?, origin_fingerprint = ?,
                    news_origin = ?, next_classification_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE post_id = ?
                """,
                (
                    post_state.value,
                    result.to_json(),
                    origin_fingerprint,
                    result.news_origin.value,
                    now_text,
                    post_id,
                ),
            )
        return bool(cursor.rowcount)

    def schedule_classification_retry(
        self, post_id: str, next_attempt_at: datetime, error_code: str
    ) -> None:
        self._update_post(
            post_id,
            state=PostState.CLASSIFICATION_RETRY.value,
            next_classification_at=isoformat_z(next_attempt_at),
            last_error=error_code[:200],
        )

    def retry_failed_classification(self, post_id: str) -> None:
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT p.state, n.post_id AS notification_post_id
                FROM posts AS p
                LEFT JOIN notifications AS n ON n.post_id = p.post_id
                WHERE p.post_id = ?
                """,
                (post_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Post does not exist: {post_id}")
            if row["state"] != PostState.CLASSIFICATION_ERROR.value:
                raise ValueError("only a classification_error Post can be retried")
            if row["notification_post_id"] is not None:
                raise ValueError("a Post with a notification cannot be reclassified")
            self._connection.execute(
                """
                UPDATE posts
                SET state = ?, classification_json = NULL,
                    origin_fingerprint = NULL, news_origin = NULL,
                    classification_attempts = 0,
                    next_classification_at = NULL, last_error = NULL,
                    updated_at = ?
                WHERE post_id = ?
                """,
                (PostState.PENDING.value, now_text, post_id),
            )

    def _update_post(self, post_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "source_key",
            "state",
            "classification_json",
            "origin_fingerprint",
            "news_origin",
            "next_classification_at",
            "last_error",
        }
        if set(fields) - allowed:
            raise ValueError("unsupported post update")
        fields["updated_at"] = isoformat_z(utc_now())
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [*fields.values(), post_id]
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE posts SET {assignments} WHERE post_id = ?",
                values,
            )

    def claim_due_notifications(
        self, now: datetime | None = None, limit: int = 50
    ) -> list[StoredNotification]:
        now_text = isoformat_z(now or utc_now())
        claimed: list[StoredNotification] = []
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT *
                FROM notifications
                WHERE status IN (?, ?)
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (
                    NotificationState.PENDING.value,
                    NotificationState.RETRY.value,
                    now_text,
                    limit,
                ),
            ).fetchall()
            for row in rows:
                updated = self._connection.execute(
                    """
                    UPDATE notifications
                    SET status = ?, attempt_count = attempt_count + 1, updated_at = ?
                    WHERE post_id = ? AND status IN (?, ?)
                    """,
                    (
                        NotificationState.SENDING.value,
                        now_text,
                        row["post_id"],
                        NotificationState.PENDING.value,
                        NotificationState.RETRY.value,
                    ),
                )
                if updated.rowcount:
                    claimed.append(
                        StoredNotification(
                            post_id=row["post_id"],
                            bark_id=row["bark_id"],
                            title=row["title"],
                            body=row["body"],
                            url=row["url"],
                            group_name=row["group_name"],
                            level=row["level"],
                            sound=row["sound"],
                            attempt_count=int(row["attempt_count"]) + 1,
                        )
                    )
        return claimed

    def mark_notification_sent(self, post_id: str) -> None:
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE notifications
                SET status = ?, sent_at = ?, last_error = NULL, next_attempt_at = NULL,
                    updated_at = ?
                WHERE post_id = ?
                """,
                (NotificationState.SENT.value, now_text, now_text, post_id),
            )
            self._connection.execute(
                "UPDATE posts SET state = ?, updated_at = ? WHERE post_id = ?",
                (PostState.NOTIFIED.value, now_text, post_id),
            )

    def schedule_notification_retry(
        self, post_id: str, next_attempt_at: datetime, error_code: str
    ) -> None:
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE notifications
                SET status = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE post_id = ?
                """,
                (
                    NotificationState.RETRY.value,
                    isoformat_z(next_attempt_at),
                    error_code[:200],
                    now_text,
                    post_id,
                ),
            )

    def mark_notification_uncertain(self, post_id: str, error_code: str) -> None:
        self._finish_notification(
            post_id,
            NotificationState.UNCERTAIN,
            PostState.NOTIFICATION_UNCERTAIN,
            error_code,
        )

    def mark_notification_failed(self, post_id: str, error_code: str) -> None:
        self._finish_notification(
            post_id,
            NotificationState.FAILED,
            PostState.NOTIFICATION_FAILED,
            error_code,
        )

    def _finish_notification(
        self,
        post_id: str,
        notification_state: NotificationState,
        post_state: PostState,
        error_code: str,
    ) -> None:
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE notifications
                SET status = ?, next_attempt_at = NULL, last_error = ?, updated_at = ?
                WHERE post_id = ?
                """,
                (notification_state.value, error_code[:200], now_text, post_id),
            )
            self._connection.execute(
                "UPDATE posts SET state = ?, last_error = ?, updated_at = ? WHERE post_id = ?",
                (post_state.value, error_code[:200], now_text, post_id),
            )

    def recover_inflight_notifications(self) -> int:
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT post_id FROM notifications WHERE status = ?",
                (NotificationState.SENDING.value,),
            ).fetchall()
            for row in rows:
                post_id = row["post_id"]
                self._connection.execute(
                    """
                    UPDATE notifications
                    SET status = ?, next_attempt_at = NULL, last_error = ?, updated_at = ?
                    WHERE post_id = ?
                    """,
                    (
                        NotificationState.UNCERTAIN.value,
                        "process_interrupted_during_delivery",
                        now_text,
                        post_id,
                    ),
                )
                self._connection.execute(
                    "UPDATE posts SET state = ?, last_error = ?, updated_at = ? WHERE post_id = ?",
                    (
                        PostState.NOTIFICATION_UNCERTAIN.value,
                        "process_interrupted_during_delivery",
                        now_text,
                        post_id,
                    ),
                )
        return len(rows)

    def resolve_uncertain_notification(self, post_id: str, action: str) -> None:
        if action not in {"assume-delivered", "retry"}:
            raise ValueError("unsupported notification resolution")
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status FROM notifications WHERE post_id = ?",
                (post_id,),
            ).fetchone()
            if row is None:
                raise ValueError("notification does not exist")
            if row["status"] not in {
                NotificationState.UNCERTAIN.value,
                NotificationState.FAILED.value,
            }:
                raise ValueError("only uncertain or failed notifications can be resolved")
            if action == "assume-delivered":
                self._connection.execute(
                    """
                    UPDATE notifications
                    SET status = ?, sent_at = ?, next_attempt_at = NULL,
                        last_error = 'operator_assumed_delivered', updated_at = ?
                    WHERE post_id = ?
                    """,
                    (NotificationState.SENT.value, now_text, now_text, post_id),
                )
                post_state = PostState.NOTIFIED.value
            else:
                self._connection.execute(
                    """
                    UPDATE notifications
                    SET status = ?, next_attempt_at = ?,
                        last_error = 'operator_authorized_retry', updated_at = ?
                    WHERE post_id = ?
                    """,
                    (NotificationState.RETRY.value, now_text, now_text, post_id),
                )
                post_state = PostState.NOTIFICATION_PENDING.value
            self._connection.execute(
                "UPDATE posts SET state = ?, updated_at = ? WHERE post_id = ?",
                (post_state, now_text, post_id),
            )

    def record_x_response(
        self,
        post_ids: Iterable[str],
        unit_price_usd: Decimal,
        operation: str = "recent_search",
        occurred_at: datetime | None = None,
    ) -> int:
        return self._record_x_resources(
            resource_type="post",
            resource_ids=post_ids,
            unit_price_usd=unit_price_usd,
            operation=operation,
            occurred_at=occurred_at,
        )

    def record_x_user_response(
        self,
        user_ids: Iterable[str],
        unit_price_usd: Decimal,
        operation: str = "user_lookup",
        occurred_at: datetime | None = None,
    ) -> int:
        return self._record_x_resources(
            resource_type="user",
            resource_ids=user_ids,
            unit_price_usd=unit_price_usd,
            operation=operation,
            occurred_at=occurred_at,
        )

    def _record_x_resources(
        self,
        *,
        resource_type: str,
        resource_ids: Iterable[str],
        unit_price_usd: Decimal,
        operation: str,
        occurred_at: datetime | None,
    ) -> int:
        current = occurred_at or utc_now()
        timestamp = isoformat_z(current)
        utc_day = current.astimezone(timezone.utc).date().isoformat()
        identifiers = tuple(resource_ids)
        new_units = 0
        with self._lock, self._connection:
            for resource_id in identifiers:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO x_resource_daily(
                        utc_day, resource_type, resource_id, first_seen_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (utc_day, resource_type, resource_id, timestamp),
                )
                new_units += cursor.rowcount
            self._insert_usage(
                provider="x",
                operation=operation,
                occurred_at=timestamp,
                status="ok",
                units=new_units,
                raw_units=len(identifiers),
                estimated_cost=Decimal(new_units) * unit_price_usd,
            )
        return new_units

    def record_api_error(self, provider: str, operation: str, status: str) -> None:
        with self._lock, self._connection:
            self._insert_usage(
                provider=provider,
                operation=operation,
                occurred_at=isoformat_z(utc_now()),
                status=status[:80],
            )

    def record_model_usage(
        self,
        usage: ModelUsage,
        estimated_cost: Decimal,
        status: str = "ok",
    ) -> None:
        with self._lock, self._connection:
            self._insert_usage(
                provider="deepseek",
                operation="chat_completions",
                occurred_at=isoformat_z(utc_now()),
                status=status,
                prompt_tokens=usage.prompt_tokens,
                prompt_cache_hit_tokens=usage.prompt_cache_hit_tokens,
                prompt_cache_miss_tokens=usage.prompt_cache_miss_tokens,
                completion_tokens=usage.completion_tokens,
                estimated_cost=estimated_cost,
            )

    def record_bark_request(self, status: str) -> None:
        with self._lock, self._connection:
            self._insert_usage(
                provider="bark",
                operation="push",
                occurred_at=isoformat_z(utc_now()),
                status=status[:80],
            )

    def _insert_usage(
        self,
        *,
        provider: str,
        operation: str,
        occurred_at: str,
        status: str,
        units: int = 0,
        raw_units: int = 0,
        prompt_tokens: int = 0,
        prompt_cache_hit_tokens: int = 0,
        prompt_cache_miss_tokens: int = 0,
        completion_tokens: int = 0,
        estimated_cost: Decimal = Decimal("0"),
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO api_usage(
                provider, operation, occurred_at, status, units, raw_units,
                prompt_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                completion_tokens, estimated_cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                operation,
                occurred_at,
                status,
                units,
                raw_units,
                prompt_tokens,
                prompt_cache_hit_tokens,
                prompt_cache_miss_tokens,
                completion_tokens,
                str(estimated_cost),
            ),
        )

    def request_count_since(self, provider: str, since: datetime) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COALESCE(SUM(request_count), 0) AS count
                FROM api_usage
                WHERE provider = ? AND occurred_at >= ?
                """,
                (provider, isoformat_z(since)),
            ).fetchone()
        return int(row["count"])

    def x_estimated_spend_since(self, since: datetime) -> Decimal:
        return self._estimated_spend_since("x", since)

    def _estimated_spend_since(self, provider: str, since: datetime) -> Decimal:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT estimated_cost_usd
                FROM api_usage
                WHERE provider = ? AND occurred_at >= ?
                """,
                (provider, isoformat_z(since)),
            ).fetchall()
        return sum((Decimal(row["estimated_cost_usd"]) for row in rows), Decimal("0"))

    def cost_snapshot(self, cycle_start: datetime) -> CostSnapshot:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT provider,
                       COALESCE(SUM(request_count), 0) AS requests,
                       COALESCE(SUM(units), 0) AS units
                FROM api_usage
                WHERE occurred_at >= ?
                GROUP BY provider
                """,
                (isoformat_z(cycle_start),),
            ).fetchall()
            x_post_row = self._connection.execute(
                """
                SELECT COALESCE(SUM(units), 0) AS units
                FROM api_usage
                WHERE provider = 'x'
                  AND operation = 'recent_search'
                  AND occurred_at >= ?
                """,
                (isoformat_z(cycle_start),),
            ).fetchone()
        aggregate = {row["provider"]: row for row in rows}
        x = aggregate.get("x")
        deepseek = aggregate.get("deepseek")
        bark = aggregate.get("bark")
        return CostSnapshot(
            cycle_start=cycle_start,
            x_post_units=int(x_post_row["units"]) if x_post_row else 0,
            x_estimated_usd=self._estimated_spend_since("x", cycle_start),
            x_requests=int(x["requests"]) if x else 0,
            deepseek_requests=int(deepseek["requests"]) if deepseek else 0,
            deepseek_estimated_usd=self._estimated_spend_since("deepseek", cycle_start),
            bark_requests=int(bark["requests"]) if bark else 0,
        )

    def usage_window(
        self, provider: str, since: datetime | None = None
    ) -> tuple[datetime | None, datetime | None]:
        where = "provider = ?"
        parameters: list[str] = [provider]
        if since is not None:
            where += " AND occurred_at >= ?"
            parameters.append(isoformat_z(since))
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at
                FROM api_usage
                WHERE {where}
                """,
                parameters,
            ).fetchone()
        first = parse_utc_datetime(row["first_at"]) if row and row["first_at"] else None
        last = parse_utc_datetime(row["last_at"]) if row and row["last_at"] else None
        return first, last

    def set_health_flag(self, key: str, severity: str, message: str, active: bool = True) -> None:
        if severity not in {"warning", "critical"}:
            raise ValueError("health severity must be warning or critical")
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO health_flags(key, severity, message, active, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    severity = excluded.severity,
                    message = excluded.message,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (key, severity, message[:500], 1 if active else 0, now_text),
            )

    def clear_health_flag(self, key: str) -> None:
        now_text = isoformat_z(utc_now())
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE health_flags SET active = 0, updated_at = ? WHERE key = ?",
                (now_text, key),
            )

    def health_report(self) -> dict[str, Any]:
        with self._lock:
            flags = self._connection.execute(
                """
                SELECT key, severity, message, updated_at
                FROM health_flags
                WHERE active = 1
                ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, key
                """
            ).fetchall()
            states = self._connection.execute(
                "SELECT state, COUNT(*) AS count FROM posts GROUP BY state"
            ).fetchall()
            notifications = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM notifications GROUP BY status"
            ).fetchall()
            cursor = self._connection.execute(
                "SELECT MAX(last_success_at) AS latest FROM query_cursors"
            ).fetchone()
            self._connection.execute("SELECT 1").fetchone()
        report_flags = [dict(row) for row in flags]
        ready = not any(flag["severity"] == "critical" for flag in report_flags)
        return {
            "live": True,
            "ready": ready,
            "database": "ok",
            "latest_x_success_at": cursor["latest"] if cursor else None,
            "post_states": {row["state"]: row["count"] for row in states},
            "notification_states": {
                row["status"]: row["count"] for row in notifications
            },
            "flags": report_flags,
        }

    def post_state(self, post_id: str) -> PostState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT state FROM posts WHERE post_id = ?", (post_id,)
            ).fetchone()
        return PostState(row["state"]) if row else None

    def notification_status(self, post_id: str) -> NotificationState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM notifications WHERE post_id = ?", (post_id,)
            ).fetchone()
        return NotificationState(row["status"]) if row else None

    def notification_count(self, statuses: tuple[NotificationState, ...] | None = None) -> int:
        with self._lock:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                row = self._connection.execute(
                    f"SELECT COUNT(*) AS count FROM notifications WHERE status IN ({placeholders})",
                    tuple(status.value for status in statuses),
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT COUNT(*) AS count FROM notifications"
                ).fetchone()
        return int(row["count"])
