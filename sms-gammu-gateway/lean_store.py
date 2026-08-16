"""SQLite-backed delivery queue for the lean modem gateway."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path
from typing import Any

from lean_assembler import LogicalMessage


class MessageStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        number TEXT NOT NULL,
                        text TEXT NOT NULL,
                        message_date TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        acknowledged_at TEXT
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS messages_pending "
                    "ON messages(acknowledged_at, created_at)"
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS physical_cleanup (
                        message_id TEXT NOT NULL,
                        location INTEGER NOT NULL,
                        fingerprint TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (message_id, location, fingerprint),
                        FOREIGN KEY (message_id) REFERENCES messages(id)
                    )
                    """
                )

    def enqueue(self, message: LogicalMessage) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO messages "
                    "(id, number, text, message_date, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (message.id, message.number, message.text, message.date, now),
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO physical_cleanup "
                    "(message_id, location, fingerprint, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            message.id,
                            int(part["Location"]),
                            str(part["Fingerprint"]),
                            now,
                        )
                        for part in message.parts
                    ],
                )
        return cursor.rowcount == 1

    def cleanup_groups(self) -> dict[str, list[dict[str, Any]]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT message_id, location, fingerprint "
                "FROM physical_cleanup ORDER BY created_at, message_id, location"
            ).fetchall()
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row["message_id"], []).append(
                {
                    "Location": int(row["location"]),
                    "Fingerprint": row["fingerprint"],
                }
            )
        return groups

    def resolve_cleanup(self, message_id: str, locations: list[int]) -> int:
        if not locations:
            return 0
        placeholders = ",".join("?" for _ in locations)
        parameters: list[Any] = [message_id, *[int(item) for item in locations]]
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM physical_cleanup WHERE message_id = ? "
                    f"AND location IN ({placeholders})",
                    parameters,
                )
        return cursor.rowcount

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, number, text, message_date, created_at "
                "FROM messages WHERE acknowledged_at IS NULL "
                "ORDER BY created_at, id LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [
            {
                "ID": row["id"],
                "Number": row["number"],
                "Text": row["text"],
                "Date": row["message_date"],
                "CreatedAt": row["created_at"],
            }
            for row in rows
        ]

    def acknowledge(self, message_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE messages SET acknowledged_at = "
                    "COALESCE(acknowledged_at, ?) WHERE id = ?",
                    (now, message_id),
                )
        return cursor.rowcount == 1

    def cleanup(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM messages WHERE acknowledged_at IS NOT NULL "
                    "AND acknowledged_at < ?",
                    (cutoff,),
                )
        return cursor.rowcount

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN acknowledged_at IS NULL THEN 1 ELSE 0 END) AS pending "
                "FROM messages"
            ).fetchone()
            cleanup = connection.execute(
                "SELECT COUNT(*) AS count FROM physical_cleanup"
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "pending": int(row["pending"] or 0),
            "physical_cleanup": int(cleanup["count"] or 0),
        }
