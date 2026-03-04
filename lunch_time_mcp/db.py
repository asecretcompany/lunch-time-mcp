"""SQLite-backed message inbox for the Signal MCP polling daemon.

Provides a durable queue between the polling daemon (writer) and the
MCP server's receive_message tool (reader).
"""

import sqlite3
import time
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("lunch-time-mcp.db")

DEFAULT_DB_PATH = Path.home() / ".lunch-time-mcp" / "inbox.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    sender_uuid TEXT NOT NULL,
    message TEXT NOT NULL,
    group_id TEXT,
    processed INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_unprocessed ON inbox(processed, created_at);
"""


@dataclass
class InboxMessage:
    """A message stored in the inbox."""

    id: int
    timestamp: float
    sender_uuid: str
    message: str
    group_id: Optional[str]
    processed: bool
    created_at: float


def init_db(db_path: Optional[str] = None) -> Path:
    """Initialize the database, creating the file and schema if needed.

    Returns the resolved path to the database file.
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        logger.info(f"Database initialized at {path}")
    finally:
        conn.close()

    return path


def insert_message(
    db_path: Path,
    timestamp: float,
    sender_uuid: str,
    message: str,
    group_id: Optional[str] = None,
) -> int:
    """Insert a new message into the inbox. Returns the row ID."""
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            "INSERT INTO inbox (timestamp, sender_uuid, message, group_id, processed, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (timestamp, sender_uuid, message, group_id, time.time()),
        )
        conn.commit()
        row_id = cursor.lastrowid
        logger.debug(f"Inserted message id={row_id} from {sender_uuid}")
        return row_id
    finally:
        conn.close()


def get_unprocessed(db_path: Path, limit: int = 50) -> list[InboxMessage]:
    """Retrieve unprocessed messages, oldest first."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, timestamp, sender_uuid, message, group_id, processed, created_at "
            "FROM inbox WHERE processed = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()

        return [
            InboxMessage(
                id=r[0],
                timestamp=r[1],
                sender_uuid=r[2],
                message=r[3],
                group_id=r[4],
                processed=bool(r[5]),
                created_at=r[6],
            )
            for r in rows
        ]
    finally:
        conn.close()


def mark_processed(db_path: Path, message_ids: list[int]) -> int:
    """Mark messages as processed. Returns the number of rows updated."""
    if not message_ids:
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        placeholders = ",".join("?" for _ in message_ids)
        cursor = conn.execute(
            f"UPDATE inbox SET processed = 1 WHERE id IN ({placeholders})",
            message_ids,
        )
        conn.commit()
        updated = cursor.rowcount
        logger.debug(f"Marked {updated} message(s) as processed")
        return updated
    finally:
        conn.close()


def get_stats(db_path: Path) -> dict:
    """Get queue statistics."""
    conn = sqlite3.connect(str(db_path))
    try:
        total = conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        unprocessed = conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE processed = 0"
        ).fetchone()[0]
        return {
            "total": total,
            "unprocessed": unprocessed,
            "processed": total - unprocessed,
        }
    finally:
        conn.close()
