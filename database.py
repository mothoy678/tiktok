"""
SQLite database layer for user and download statistics.
"""

from __future__ import annotations

import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATABASE_PATH


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Async SQLite helper for users and downloads."""

    def __init__(self, db_path: Path = DATABASE_PATH) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_tables(self) -> None:
        assert self._conn is not None
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                media_type TEXT NOT NULL,
                quality TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_downloads_user
                ON downloads(telegram_user_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_status
                ON downloads(status);
            """
        )
        await self._conn.commit()

    async def upsert_user(
        self,
        telegram_user_id: int,
        username: str | None = None,
    ) -> None:
        assert self._conn is not None
        now = _utcnow()
        await self._conn.execute(
            """
            INSERT INTO users (telegram_user_id, username, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username),
                last_seen = excluded.last_seen
            """,
            (telegram_user_id, username, now, now),
        )
        await self._conn.commit()

    async def log_download(
        self,
        telegram_user_id: int,
        url: str,
        media_type: str,
        quality: str | None,
        status: str,
    ) -> int:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """
            INSERT INTO downloads
                (telegram_user_id, url, media_type, quality, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (telegram_user_id, url, media_type, quality, status, _utcnow()),
        )
        await self._conn.commit()
        return cursor.lastrowid or 0

    async def update_download_status(
        self,
        download_id: int,
        status: str,
    ) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE downloads SET status = ? WHERE id = ?",
            (status, download_id),
        )
        await self._conn.commit()

    async def get_stats(self) -> dict[str, Any]:
        assert self._conn is not None
        stats: dict[str, Any] = {}

        async with self._conn.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            stats["users"] = row[0] if row else 0

        async with self._conn.execute("SELECT COUNT(*) FROM downloads") as cur:
            row = await cur.fetchone()
            stats["downloads"] = row[0] if row else 0

        async with self._conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE status = 'success'"
        ) as cur:
            row = await cur.fetchone()
            stats["success"] = row[0] if row else 0

        async with self._conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE status = 'failed'"
        ) as cur:
            row = await cur.fetchone()
            stats["failed"] = row[0] if row else 0

        async with self._conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE media_type = 'video' AND status = 'success'"
        ) as cur:
            row = await cur.fetchone()
            stats["videos"] = row[0] if row else 0

        async with self._conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE media_type = 'music' AND status = 'success'"
        ) as cur:
            row = await cur.fetchone()
            stats["music"] = row[0] if row else 0

        return stats

    async def get_recent_users(self, limit: int = 20) -> list[dict[str, Any]]:
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT telegram_user_id, username, first_seen, last_seen
            FROM users
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# Global instance
db = Database()
