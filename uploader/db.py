"""aiosqlite-backed persistence: jobs, channels, upload history and key/value settings."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER UNIQUE NOT NULL,
    title TEXT,
    username TEXT,
    caption_template TEXT DEFAULT '',
    footer TEXT DEFAULT '',
    is_default INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_chat_id INTEGER NOT NULL,
    status_message_id INTEGER,
    url TEXT NOT NULL,
    caption TEXT DEFAULT '',
    channel_ids TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    file_path TEXT,
    filename TEXT,
    file_size INTEGER DEFAULT 0,
    error TEXT,
    url_hash TEXT,
    file_hash TEXT,
    options TEXT DEFAULT '{}',
    batch_id TEXT,
    batch_index INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_url_hash ON jobs(url_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_batch_id ON jobs(batch_id);

CREATE TABLE IF NOT EXISTS uploads_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    url TEXT,
    filename TEXT,
    file_size INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    file_hash TEXT,
    url_hash TEXT,
    uploaded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_file_hash ON uploads_history(file_hash);
CREATE INDEX IF NOT EXISTS idx_history_url_hash ON uploads_history(url_hash);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {
    "force_document": "0",
    "auto_thumbnail": "1",
    "split_mode": "1",
    "download_concurrency": "2",
    "upload_concurrency": "1",
    "default_caption_template": "{filename}\n{size} | {duration} | {resolution}",
    "default_footer": "",
}

ACTIVE_STATUSES = ("queued", "downloading", "uploading", "paused")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: int
    admin_chat_id: int
    status_message_id: Optional[int]
    url: str
    caption: str
    channel_ids: list[int]
    status: str
    file_path: Optional[str]
    filename: Optional[str]
    file_size: int
    error: Optional[str]
    url_hash: Optional[str]
    file_hash: Optional[str]
    options: dict
    batch_id: Optional[str]
    batch_index: Optional[int]
    created_at: str
    updated_at: str

    @staticmethod
    def from_row(row: aiosqlite.Row) -> "Job":
        return Job(
            id=row["id"],
            admin_chat_id=row["admin_chat_id"],
            status_message_id=row["status_message_id"],
            url=row["url"],
            caption=row["caption"] or "",
            channel_ids=json.loads(row["channel_ids"] or "[]"),
            status=row["status"],
            file_path=row["file_path"],
            filename=row["filename"],
            file_size=row["file_size"] or 0,
            error=row["error"],
            url_hash=row["url_hash"],
            file_hash=row["file_hash"],
            options=json.loads(row["options"] or "{}"),
            batch_id=row["batch_id"],
            batch_index=row["batch_index"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class Channel:
    id: int
    chat_id: int
    title: str
    username: Optional[str]
    caption_template: str
    footer: str
    is_default: bool

    @staticmethod
    def from_row(row: aiosqlite.Row) -> "Channel":
        return Channel(
            id=row["id"],
            chat_id=row["chat_id"],
            title=row["title"] or "",
            username=row["username"],
            caption_template=row["caption_template"] or "",
            footer=row["footer"] or "",
            is_default=bool(row["is_default"]),
        )


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        for ddl in (
            "ALTER TABLE jobs ADD COLUMN batch_id TEXT",
            "ALTER TABLE jobs ADD COLUMN batch_index INTEGER",
        ):
            try:
                await self._conn.execute(ddl)
            except aiosqlite.OperationalError:
                pass  # column already exists (fresh schema already includes it)
        for key, value in DEFAULT_SETTINGS.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    # --- jobs -----------------------------------------------------------

    async def create_job(
        self,
        admin_chat_id: int,
        url: str,
        caption: str,
        channel_ids: list[int],
        url_hash: str,
        options: Optional[dict] = None,
        batch_id: Optional[str] = None,
        batch_index: Optional[int] = None,
    ) -> int:
        now = _now()
        cur = await self.conn.execute(
            """INSERT INTO jobs (admin_chat_id, url, caption, channel_ids, status,
                                 url_hash, options, batch_id, batch_index, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
            (
                admin_chat_id,
                url,
                caption,
                json.dumps(channel_ids),
                url_hash,
                json.dumps(options or {}),
                batch_id,
                batch_index,
                now,
                now,
            ),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_jobs_by_batch(self, batch_id: str) -> list[Job]:
        """Ordered by the position each URL had in the admin's original message,
        not by download-completion order, so albums preserve link order."""
        cur = await self.conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? ORDER BY batch_index ASC, id ASC", (batch_id,)
        )
        rows = await cur.fetchall()
        return [Job.from_row(r) for r in rows]

    async def get_job(self, job_id: int) -> Optional[Job]:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return Job.from_row(row) if row else None

    async def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        if "channel_ids" in fields and isinstance(fields["channel_ids"], list):
            fields["channel_ids"] = json.dumps(fields["channel_ids"])
        if "options" in fields and isinstance(fields["options"], dict):
            fields["options"] = json.dumps(fields["options"])
        columns = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [job_id]
        await self.conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", values)
        await self.conn.commit()

    async def list_jobs(self, statuses: Optional[tuple] = None, limit: int = 50) -> list[Job]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            cur = await self.conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (*statuses, limit),
            )
        else:
            cur = await self.conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [Job.from_row(r) for r in rows]

    async def next_queued_job(self) -> Optional[Job]:
        cur = await self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        )
        row = await cur.fetchone()
        return Job.from_row(row) if row else None

    async def reset_stuck_jobs(self) -> int:
        """On startup, move jobs stuck mid-flight back to 'queued' so workers resume them."""
        cur = await self.conn.execute(
            "UPDATE jobs SET status = 'queued', updated_at = ? WHERE status IN ('downloading', 'uploading')",
            (_now(),),
        )
        await self.conn.commit()
        return cur.rowcount

    async def delete_job(self, job_id: int) -> None:
        await self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await self.conn.commit()

    # --- channels ---------------------------------------------------------

    async def add_channel(self, chat_id: int, title: str, username: Optional[str]) -> int:
        existing_count = await self.conn.execute("SELECT COUNT(*) FROM channels")
        row = await existing_count.fetchone()
        is_default = 1 if row[0] == 0 else 0
        cur = await self.conn.execute(
            """INSERT INTO channels (chat_id, title, username, is_default)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, username = excluded.username""",
            (chat_id, title, username, is_default),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def remove_channel(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    async def list_channels(self) -> list[Channel]:
        cur = await self.conn.execute("SELECT * FROM channels ORDER BY id ASC")
        rows = await cur.fetchall()
        return [Channel.from_row(r) for r in rows]

    async def get_default_channel(self) -> Optional[Channel]:
        cur = await self.conn.execute("SELECT * FROM channels WHERE is_default = 1 LIMIT 1")
        row = await cur.fetchone()
        return Channel.from_row(row) if row else None

    async def set_default_channel(self, chat_id: int) -> None:
        await self.conn.execute("UPDATE channels SET is_default = 0")
        await self.conn.execute("UPDATE channels SET is_default = 1 WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    async def set_channel_caption(self, chat_id: int, template: str) -> None:
        await self.conn.execute(
            "UPDATE channels SET caption_template = ? WHERE chat_id = ?", (template, chat_id)
        )
        await self.conn.commit()

    async def set_channel_footer(self, chat_id: int, footer: str) -> None:
        await self.conn.execute("UPDATE channels SET footer = ? WHERE chat_id = ?", (footer, chat_id))
        await self.conn.commit()

    # --- upload history / dedup -------------------------------------------

    async def record_upload(
        self,
        job_id: int,
        url: str,
        filename: str,
        file_size: int,
        channel_id: int,
        message_id: int,
        file_hash: Optional[str],
        url_hash: Optional[str],
    ) -> None:
        await self.conn.execute(
            """INSERT INTO uploads_history
               (job_id, url, filename, file_size, channel_id, message_id, file_hash, url_hash, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, url, filename, file_size, channel_id, message_id, file_hash, url_hash, _now()),
        )
        await self.conn.commit()

    async def find_duplicate(self, url_hash: str, file_hash: Optional[str]) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM uploads_history WHERE url_hash = ? ORDER BY id DESC LIMIT 1", (url_hash,)
        )
        row = await cur.fetchone()
        if row:
            return row
        if file_hash:
            cur = await self.conn.execute(
                "SELECT * FROM uploads_history WHERE file_hash = ? ORDER BY id DESC LIMIT 1", (file_hash,)
            )
            row = await cur.fetchone()
        return row

    async def list_history(self, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM uploads_history ORDER BY id DESC LIMIT ?", (limit,)
        )
        return list(await cur.fetchall())

    async def stats(self) -> dict:
        cur = await self.conn.execute("SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM uploads_history")
        count, total_bytes = await cur.fetchone()
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status"
        )
        by_status = {row[0]: row[1] for row in await cur.fetchall()}
        return {"uploads": count, "total_bytes": total_bytes, "jobs_by_status": by_status}

    # --- settings -----------------------------------------------------------

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def all_settings(self) -> dict:
        cur = await self.conn.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}
