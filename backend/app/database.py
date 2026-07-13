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
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_tasks (
  id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL,
  status TEXT NOT NULL, prompt TEXT NOT NULL, termination_reason TEXT,
  model_calls INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
  files_modified INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
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
  task_id TEXT, source TEXT NOT NULL DEFAULT 'builtin', risk TEXT,
  confirmed INTEGER NOT NULL DEFAULT 0,
  tool TEXT NOT NULL, status TEXT NOT NULL, input TEXT, output TEXT,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL, duration_ms INTEGER,
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
CREATE TABLE IF NOT EXISTS skill_settings (
  path TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA busy_timeout = 15000")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def init_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        db.execute("UPDATE conversations SET permission_mode='ask' WHERE permission_mode IN ('readonly','confirm')")
        db.execute("UPDATE conversations SET permission_mode='full' WHERE permission_mode='auto'")
        db.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)", (now_iso(),))
        db.execute("UPDATE agent_tasks SET status='interrupted', termination_reason='应用上次运行时中断', updated_at=? WHERE status IN ('pending','running')", (now_iso(),))
        existing = {row[1] for row in db.execute("PRAGMA table_info(tool_runs)")}
        for column, definition in {
            "task_id": "TEXT", "source": "TEXT NOT NULL DEFAULT 'builtin'", "risk": "TEXT",
            "confirmed": "INTEGER NOT NULL DEFAULT 0", "duration_ms": "INTEGER",
        }.items():
            if column not in existing:
                db.execute(f"ALTER TABLE tool_runs ADD COLUMN {column} {definition}")


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]


def audit(conversation_id: int | None, action: str, target: str, status: str, details: Any = None) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO audit_logs(conversation_id, action, target, status, details, created_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, action, target, status, json.dumps(sanitize_details(details), ensure_ascii=False) if details is not None else None, now_iso()),
        )


def sanitize_details(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("key", "token", "secret", "password", "authorization")):
                cleaned[key] = "***"
            elif key == "content" and isinstance(item, str):
                cleaned[key] = f"<content {len(item.encode('utf-8'))} bytes>"
            else: cleaned[key] = sanitize_details(item)
        return cleaned
    if isinstance(value, list): return [sanitize_details(item) for item in value[:100]]
    if isinstance(value, str) and len(value) > 2000: return value[:2000] + "…"
    return value


def backup_database() -> dict[str, Any]:
    source = Path(settings.database_path)
    source.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = source.parent / "backups"; backup_dir.mkdir(exist_ok=True)
    target = backup_dir / f"agent-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    with connect() as db, sqlite3.connect(target) as destination:
        db.backup(destination)
    return {"name": target.name, "size": target.stat().st_size, "created_at": now_iso()}


def database_backups() -> list[dict[str, Any]]:
    folder = Path(settings.database_path).parent / "backups"
    if not folder.exists(): return []
    return [{"name": path.name, "size": path.stat().st_size, "modified_at": path.stat().st_mtime} for path in sorted(folder.glob("agent-*.db"), reverse=True)]


def restore_database(name: str) -> dict[str, Any]:
    if Path(name).name != name or not name.startswith("agent-") or not name.endswith(".db"):
        raise ValueError("无效的备份名称")
    source = Path(settings.database_path).parent / "backups" / name
    if not source.exists(): raise ValueError("备份不存在")
    safety = backup_database()
    with sqlite3.connect(source) as backup, connect() as destination:
        backup.backup(destination)
    init_db()
    return {"restored": name, "safety_backup": safety["name"]}
