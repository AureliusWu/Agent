import json
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings


SCHEMA_VERSION = 8


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
  files_modified INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
  repair_attempts INTEGER NOT NULL DEFAULT 0, verification_attempts INTEGER NOT NULL DEFAULT 0,
  current_phase TEXT NOT NULL DEFAULT 'analysis', checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
  resume_count INTEGER NOT NULL DEFAULT 0, resumable INTEGER NOT NULL DEFAULT 1, paused_at TEXT,
  current_step TEXT, completed_steps TEXT NOT NULL DEFAULT '[]', pending_steps TEXT NOT NULL DEFAULT '[]',
  last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT,
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
  structured_state TEXT NOT NULL DEFAULT '{}',
  compacted_through INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tool_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
  task_id TEXT, source TEXT NOT NULL DEFAULT 'builtin', risk TEXT,
  execution_id TEXT UNIQUE,
  confirmed INTEGER NOT NULL DEFAULT 0,
  tool TEXT NOT NULL, status TEXT NOT NULL, input TEXT, output TEXT,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL, duration_ms INTEGER,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS model_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, task_id TEXT,
  provider TEXT NOT NULL, model TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL, duration_ms INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0, success INTEGER NOT NULL,
  error_type TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS approval_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL,
  conversation_id INTEGER, task_id TEXT, tool TEXT NOT NULL, arguments_hash TEXT NOT NULL,
  risk TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at TEXT,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL, summary TEXT NOT NULL, report TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_plans (
  task_id TEXT PRIMARY KEY, status TEXT NOT NULL, plan TEXT NOT NULL,
  acceptance_criteria TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_verification_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  status TEXT NOT NULL, report TEXT NOT NULL, evidence_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(task_id, attempt),
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_repair_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  status TEXT NOT NULL, retry_scope TEXT NOT NULL, before_fingerprint TEXT NOT NULL,
  after_fingerprint TEXT, reason TEXT, created_at TEXT NOT NULL, finished_at TEXT,
  UNIQUE(task_id, attempt),
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  phase TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL,
  workspace_hash TEXT NOT NULL, git_status TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  UNIQUE(task_id, sequence),
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_operations (
  execution_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
  tool_call_id TEXT NOT NULL, tool TEXT NOT NULL, arguments_hash TEXT NOT NULL,
  status TEXT NOT NULL, result TEXT, side_effect INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL, finished_at TEXT,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_working_memory (
  task_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
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
CREATE TABLE IF NOT EXISTS skill_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, name TEXT NOT NULL,
  path TEXT NOT NULL, content_chars INTEGER NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS workspace_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, key TEXT NOT NULL,
  content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'project', source TEXT NOT NULL DEFAULT 'user',
  source_task_id TEXT, tags TEXT NOT NULL DEFAULT '[]', applicable_version TEXT,
  project_signature TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL DEFAULT 0.7,
  last_verified_at TEXT, last_used_at TEXT, use_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
  rejected INTEGER NOT NULL DEFAULT 0, invalidated_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(workspace, key),
  FOREIGN KEY(source_task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL
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
    db.execute("PRAGMA busy_timeout = 15000")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _migration_v2(db: sqlite3.Connection) -> None:
    existing = {row[1] for row in db.execute("PRAGMA table_info(tool_runs)")}
    for column, definition in {
            "task_id": "TEXT", "source": "TEXT NOT NULL DEFAULT 'builtin'", "risk": "TEXT",
            "confirmed": "INTEGER NOT NULL DEFAULT 0", "duration_ms": "INTEGER",
    }.items():
        if column not in existing:
            db.execute(f"ALTER TABLE tool_runs ADD COLUMN {column} {definition}")
    task_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_tasks)")}
    for column, definition in {
            "total_tokens": "INTEGER NOT NULL DEFAULT 0",
            "current_step": "TEXT",
            "completed_steps": "TEXT NOT NULL DEFAULT '[]'",
            "pending_steps": "TEXT NOT NULL DEFAULT '[]'",
            "started_at": "TEXT",
            "finished_at": "TEXT",
    }.items():
        if column not in task_columns:
            db.execute(f"ALTER TABLE agent_tasks ADD COLUMN {column} {definition}")
    db.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_conversation ON agent_tasks(conversation_id, created_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tool_runs_task ON tool_runs(task_id, id DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_model_runs_task ON model_runs(task_id, id DESC)")


def _migration_v3(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS idx_approval_grants_context ON approval_grants(conversation_id, task_id, expires_at)")


def _migration_v4(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS idx_task_verifications_status ON task_verifications(status, created_at DESC)")


def _migration_v5(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS idx_skill_runs_task ON skill_runs(task_id, id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_workspace_memories_workspace ON workspace_memories(workspace, updated_at DESC)")


def _migration_v6(db: sqlite3.Connection) -> None:
    task_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_tasks)")}
    for column, definition in {
        "repair_attempts": "INTEGER NOT NULL DEFAULT 0",
        "verification_attempts": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if column not in task_columns:
            db.execute(f"ALTER TABLE agent_tasks ADD COLUMN {column} {definition}")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_plans (
          task_id TEXT PRIMARY KEY, status TEXT NOT NULL, plan TEXT NOT NULL,
          acceptance_criteria TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS task_verification_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
          status TEXT NOT NULL, report TEXT NOT NULL, evidence_fingerprint TEXT NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(task_id, attempt),
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS task_repair_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
          status TEXT NOT NULL, retry_scope TEXT NOT NULL, before_fingerprint TEXT NOT NULL,
          after_fingerprint TEXT, reason TEXT, created_at TEXT NOT NULL, finished_at TEXT,
          UNIQUE(task_id, attempt),
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_task_verification_attempts_task ON task_verification_attempts(task_id, attempt);
        CREATE INDEX IF NOT EXISTS idx_task_repair_runs_task ON task_repair_runs(task_id, attempt);
        """
    )


def _migration_v7(db: sqlite3.Connection) -> None:
    task_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_tasks)")}
    for column, definition in {
        "current_phase": "TEXT NOT NULL DEFAULT 'analysis'",
        "checkpoint_sequence": "INTEGER NOT NULL DEFAULT 0",
        "resume_count": "INTEGER NOT NULL DEFAULT 0",
        "resumable": "INTEGER NOT NULL DEFAULT 1",
        "paused_at": "TEXT",
    }.items():
        if column not in task_columns:
            db.execute(f"ALTER TABLE agent_tasks ADD COLUMN {column} {definition}")
    tool_columns = {row[1] for row in db.execute("PRAGMA table_info(tool_runs)")}
    if "execution_id" not in tool_columns:
        db.execute("ALTER TABLE tool_runs ADD COLUMN execution_id TEXT")
    db.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_runs_execution ON tool_runs(execution_id) WHERE execution_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS task_checkpoints (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
          phase TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL,
          workspace_hash TEXT NOT NULL, git_status TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          UNIQUE(task_id, sequence),
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS task_operations (
          execution_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
          tool_call_id TEXT NOT NULL, tool TEXT NOT NULL, arguments_hash TEXT NOT NULL,
          status TEXT NOT NULL, result TEXT, side_effect INTEGER NOT NULL DEFAULT 0,
          started_at TEXT NOT NULL, finished_at TEXT,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_task_checkpoints_task ON task_checkpoints(task_id, sequence DESC);
        CREATE INDEX IF NOT EXISTS idx_task_operations_task ON task_operations(task_id, started_at);
        """
    )


def _migration_v8(db: sqlite3.Connection) -> None:
    context_columns = {row[1] for row in db.execute("PRAGMA table_info(conversation_context)")}
    if "structured_state" not in context_columns:
        db.execute("ALTER TABLE conversation_context ADD COLUMN structured_state TEXT NOT NULL DEFAULT '{}'")
    memory_columns = {row[1] for row in db.execute("PRAGMA table_info(workspace_memories)")}
    for column, definition in {
        "kind": "TEXT NOT NULL DEFAULT 'project'",
        "source": "TEXT NOT NULL DEFAULT 'user'",
        "tags": "TEXT NOT NULL DEFAULT '[]'",
        "applicable_version": "TEXT",
        "project_signature": "TEXT NOT NULL DEFAULT '{}'",
        "confidence": "REAL NOT NULL DEFAULT 0.7",
        "last_verified_at": "TEXT",
        "last_used_at": "TEXT",
        "use_count": "INTEGER NOT NULL DEFAULT 0",
        "success_count": "INTEGER NOT NULL DEFAULT 0",
        "failure_count": "INTEGER NOT NULL DEFAULT 0",
        "rejected": "INTEGER NOT NULL DEFAULT 0",
        "invalidated_reason": "TEXT",
    }.items():
        if column not in memory_columns:
            db.execute(f"ALTER TABLE workspace_memories ADD COLUMN {column} {definition}")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_working_memory (
          task_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_workspace_memories_retrieval
          ON workspace_memories(workspace, kind, rejected, confidence, updated_at DESC);
        """
    )


MIGRATIONS = (
    (2, _migration_v2),
    (3, _migration_v3),
    (4, _migration_v4),
    (5, _migration_v5),
    (6, _migration_v6),
    (7, _migration_v7),
    (8, _migration_v8),
)


def init_db() -> None:
    with connect() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)", (now_iso(),))
        applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
        for version, migration in MIGRATIONS:
            if version not in applied:
                migration(db)
                db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (version, now_iso()))
        db.execute("UPDATE conversations SET permission_mode='ask' WHERE permission_mode IN ('readonly','confirm')")
        db.execute("UPDATE conversations SET permission_mode='full' WHERE permission_mode='auto'")
        db.execute(
            "UPDATE agent_tasks SET status='interrupted', termination_reason='应用上次运行时中断，可从最近检查点继续', "
            "resumable=1, paused_at=?, updated_at=? WHERE status IN ('pending','running')",
            (now_iso(), now_iso()),
        )
        db.execute("DELETE FROM approval_grants WHERE expires_at < ?", (time.time(),))
        db.execute("DELETE FROM audit_logs WHERE id NOT IN (SELECT id FROM audit_logs ORDER BY id DESC LIMIT 10000)")


def database_status() -> dict[str, Any]:
    with connect() as db:
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        version = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    return {"status": "ok" if integrity == "ok" and version == SCHEMA_VERSION else "error", "integrity": integrity, "schema_version": version, "expected_schema_version": SCHEMA_VERSION}


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]


def audit(conversation_id: int | None, action: str, target: str, status: str, details: Any = None) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO audit_logs(conversation_id, action, target, status, details, created_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, action, target, status, json.dumps(sanitize_details(details), ensure_ascii=False) if details is not None else None, now_iso()),
        )


def record_model_run(
    *,
    conversation_id: int | None,
    task_id: str | None,
    provider: str,
    model: str,
    started_at: str,
    duration_ms: int,
    usage: dict[str, Any],
    success: bool,
    error_type: str | None,
    retry_count: int,
) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO model_runs(conversation_id, task_id, provider, model, started_at, finished_at, duration_ms, input_tokens, output_tokens, total_tokens, success, error_type, retry_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                task_id,
                provider,
                model,
                started_at,
                now_iso(),
                duration_ms,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                int(success),
                error_type,
                retry_count,
            ),
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
    target = backup_dir / f"agent-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
    with connect() as db, closing(sqlite3.connect(target)) as destination:
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
    with closing(sqlite3.connect(source)) as backup, connect() as destination:
        backup.backup(destination)
    init_db()
    return {"restored": name, "safety_backup": safety["name"]}
