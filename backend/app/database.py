import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
  workspace TEXT NOT NULL, permission_mode TEXT NOT NULL DEFAULT 'confirm',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL, tool_calls TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS conversation_context (
  conversation_id INTEGER PRIMARY KEY, summary TEXT NOT NULL DEFAULT '',
  compacted_through INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tool_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
  tool TEXT NOT NULL, status TEXT NOT NULL, input TEXT, output TEXT,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER,
  action TEXT NOT NULL, target TEXT, status TEXT NOT NULL,
  details TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_servers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
  transport TEXT NOT NULL, url TEXT, command TEXT, args TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def init_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]


def audit(conversation_id: int | None, action: str, target: str, status: str, details: Any = None) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO audit_logs(conversation_id, action, target, status, details, created_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, action, target, status, json.dumps(details, ensure_ascii=False) if details is not None else None, now_iso()),
        )
