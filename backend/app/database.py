import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .runtime_paths import database_backup_directory
from .trust import redact_payload


SCHEMA_VERSION = 27


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
  active_identity_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, version INTEGER NOT NULL,
  payload TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL,
  actor_id TEXT NOT NULL, administrator_confirmed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, UNIQUE(agent_id, version),
  FOREIGN KEY(agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, user_id TEXT,
  memory_type TEXT NOT NULL, title TEXT, content TEXT NOT NULL, normalized_content TEXT NOT NULL,
  source_type TEXT NOT NULL, source_conversation_id TEXT, source_message_id TEXT,
  confidence REAL NOT NULL DEFAULT 0.5, importance REAL NOT NULL DEFAULT 0.5,
  emotional_weight REAL NOT NULL DEFAULT 0.0, access_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, occurred_at TEXT,
  valid_from TEXT, valid_until TEXT, last_accessed_at TEXT,
  status TEXT NOT NULL DEFAULT 'active', supersedes_memory_id TEXT,
  user_confirmed INTEGER NOT NULL DEFAULT 0, is_locked INTEGER NOT NULL DEFAULT 0,
  is_sensitive INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS memory_candidates (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, user_id TEXT,
  memory_type TEXT NOT NULL, content TEXT NOT NULL, reason TEXT NOT NULL,
  confidence REAL NOT NULL, importance REAL NOT NULL,
  is_sensitive INTEGER NOT NULL DEFAULT 0, source_conversation_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending', decision_reason TEXT,
  created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS affect_states (
  agent_id TEXT PRIMARY KEY, trait_json TEXT NOT NULL, mood_json TEXT NOT NULL,
  emotion_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS affect_events (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL, source_conversation_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relationship_states (
  agent_id TEXT NOT NULL, user_id TEXT NOT NULL, state_json TEXT NOT NULL,
  shared_history_count INTEGER NOT NULL DEFAULT 0, last_meaningful_event_id TEXT,
  updated_at TEXT NOT NULL, PRIMARY KEY(agent_id, user_id)
);
CREATE TABLE IF NOT EXISTS relationship_events (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, user_id TEXT NOT NULL,
  importance TEXT NOT NULL, delta_json TEXT NOT NULL, reason TEXT NOT NULL,
  source_conversation_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_assemblies (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, conversation_id INTEGER, task_id TEXT,
  model TEXT, token_budget INTEGER NOT NULL, estimated_tokens INTEGER NOT NULL,
  layer_summary_json TEXT NOT NULL, memory_ids_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_context_states (
  task_id TEXT PRIMARY KEY, compiler_version INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
  state_json TEXT NOT NULL, state_fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_decision_ledger (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, source TEXT NOT NULL,
  summary TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_decision_ledger_task ON task_decision_ledger(task_id, created_at);
CREATE TABLE IF NOT EXISTS consolidation_runs (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, trigger_type TEXT NOT NULL,
  status TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS continuity_snapshots (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, summary TEXT NOT NULL,
  source_memory_ids_json TEXT NOT NULL, source_task_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
  workspace TEXT NOT NULL, permission_mode TEXT NOT NULL DEFAULT 'confirm',
  agent_id TEXT NOT NULL DEFAULT 'natsume-kokoro-001',
  agent_profile_id TEXT NOT NULL DEFAULT 'general',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_tasks (
  id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL,
  agent_id TEXT NOT NULL DEFAULT 'natsume-kokoro-001',
  status TEXT NOT NULL, prompt TEXT NOT NULL, termination_reason TEXT,
  model_calls INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
  files_modified INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  phase_tokens TEXT NOT NULL DEFAULT '{}', estimated_cost_usd REAL NOT NULL DEFAULT 0,
  model_route TEXT NOT NULL DEFAULT '{}', cache_hits INTEGER NOT NULL DEFAULT 0,
  cache_misses INTEGER NOT NULL DEFAULT 0,
  orchestration_mode TEXT NOT NULL DEFAULT 'single', child_agent_count INTEGER NOT NULL DEFAULT 0,
  agent_profile_id TEXT NOT NULL DEFAULT 'general',
  agent_profile_snapshot TEXT NOT NULL DEFAULT '{}',
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
  agent_id TEXT NOT NULL DEFAULT 'natsume-kokoro-001',
  role TEXT NOT NULL, content TEXT NOT NULL, tool_calls TEXT,
  task_id TEXT, reasoning_content TEXT,
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
  agent_id TEXT NOT NULL DEFAULT 'natsume-kokoro-001',
  provider TEXT NOT NULL, model TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL, duration_ms INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0, success INTEGER NOT NULL,
  phase TEXT NOT NULL DEFAULT 'analysis', route_tier TEXT NOT NULL DEFAULT 'medium',
  task_type TEXT NOT NULL DEFAULT 'general', route_confidence REAL NOT NULL DEFAULT 0,
  max_output_tokens INTEGER NOT NULL DEFAULT 0, estimated_cost_usd REAL NOT NULL DEFAULT 0,
  context_window_tokens INTEGER NOT NULL DEFAULT 0, reserved_output_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_input_tokens INTEGER NOT NULL DEFAULT 0, input_estimate INTEGER NOT NULL DEFAULT 0,
  error_type TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS provider_capabilities (
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, endpoint_hash TEXT NOT NULL,
  model TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unknown', capabilities TEXT NOT NULL DEFAULT '{}',
  latency_ms INTEGER, sample_count INTEGER NOT NULL DEFAULT 0, success_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, observed_at TEXT NOT NULL, expires_at REAL NOT NULL,
  UNIQUE(provider, endpoint_hash, model)
);
CREATE TABLE IF NOT EXISTS approval_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL,
  conversation_id INTEGER, task_id TEXT, tool TEXT NOT NULL, arguments_hash TEXT NOT NULL,
  workspace TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '{}',
  risk TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at TEXT,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS admin_action_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL,
  operation TEXT NOT NULL, target_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
  ui_session_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at REAL NOT NULL,
  consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_admin_action_grants_active
  ON admin_action_grants(operation, target_id, expires_at, consumed_at);
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
CREATE TABLE IF NOT EXISTS task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  event_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_leases (
  task_id TEXT PRIMARY KEY, owner_instance_id TEXT NOT NULL, owner_pid INTEGER NOT NULL,
  token_hash TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
  acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at REAL NOT NULL,
  released_at TEXT, status TEXT NOT NULL DEFAULT 'active',
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_leases_active ON task_leases(status, expires_at);
CREATE TABLE IF NOT EXISTS managed_processes (
  pid INTEGER PRIMARY KEY, task_id TEXT NOT NULL, owner_pid INTEGER NOT NULL,
  owner_instance_id TEXT NOT NULL, process_identity TEXT NOT NULL,
  command_hash TEXT NOT NULL, started_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running', stopped_at TEXT,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_managed_processes_task ON managed_processes(task_id, status);
CREATE TABLE IF NOT EXISTS execution_segments (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  status TEXT NOT NULL, reason TEXT NOT NULL, phase TEXT NOT NULL,
  completed_steps TEXT NOT NULL DEFAULT '[]', pending_steps TEXT NOT NULL DEFAULT '[]',
  files_modified TEXT NOT NULL DEFAULT '[]', tool_result_refs TEXT NOT NULL DEFAULT '[]',
  context_summary TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
  model_calls INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL, finished_at TEXT,
  UNIQUE(task_id, sequence),
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_execution_segments_task ON execution_segments(task_id, sequence);
CREATE TABLE IF NOT EXISTS workspace_instruction_snapshots (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workspace TEXT NOT NULL,
  source_path TEXT NOT NULL, scope_path TEXT NOT NULL, priority INTEGER NOT NULL,
  content_hash TEXT NOT NULL, content_chars INTEGER NOT NULL,
  override INTEGER NOT NULL DEFAULT 0, findings TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_instruction_snapshots_task ON workspace_instruction_snapshots(task_id, priority);
CREATE TABLE IF NOT EXISTS conversation_queue_items (
  id TEXT PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  task_id TEXT REFERENCES agent_tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('submit','resume','steer','system')),
  priority TEXT NOT NULL CHECK(priority IN ('now','next','later')),
  priority_value INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','claimed','consumed','cancelled')),
  content TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  target_scope TEXT NOT NULL DEFAULT 'conversation',
  target_agent_id TEXT,
  claimed_at TEXT,
  consumed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_queue_pending
  ON conversation_queue_items(status, priority_value, created_at);
CREATE INDEX IF NOT EXISTS idx_conversation_queue_conversation
  ON conversation_queue_items(conversation_id, status, priority_value, created_at);
CREATE TABLE IF NOT EXISTS task_artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
  tool_call_id TEXT NOT NULL,
  media_type TEXT NOT NULL,
  path TEXT NOT NULL,
  total_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_task ON task_artifacts(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, id);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER,
  agent_id TEXT NOT NULL DEFAULT 'natsume-kokoro-001',
  action TEXT NOT NULL, target TEXT, status TEXT NOT NULL,
  details TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_flow_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, task_id TEXT,
  source TEXT NOT NULL, sink TEXT NOT NULL, classification TEXT NOT NULL,
  fields TEXT NOT NULL DEFAULT '[]', redactions INTEGER NOT NULL DEFAULT 0,
  allowed INTEGER NOT NULL DEFAULT 1, reason TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS security_snapshots (
  id TEXT PRIMARY KEY, conversation_id INTEGER, task_id TEXT, workspace TEXT NOT NULL,
  reason TEXT NOT NULL, status TEXT NOT NULL, manifest_path TEXT NOT NULL,
  file_count INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0,
  database_backup TEXT, workspace_hash TEXT NOT NULL, created_at TEXT NOT NULL, restored_at TEXT,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY, parent_task_id TEXT NOT NULL, parent_agent_id TEXT,
  role TEXT NOT NULL, orchestration_mode TEXT NOT NULL, status TEXT NOT NULL,
  objective TEXT NOT NULL, expected_output TEXT NOT NULL DEFAULT '', output TEXT,
  token_budget INTEGER NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
  tool_allowlist TEXT NOT NULL DEFAULT '[]', file_scope TEXT NOT NULL DEFAULT '[]',
  timeout_seconds INTEGER NOT NULL, risk_level TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 1,
  error TEXT, started_at TEXT NOT NULL, finished_at TEXT,
  FOREIGN KEY(parent_task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE,
  FOREIGN KEY(parent_agent_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_trace_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, parent_task_id TEXT NOT NULL, agent_run_id TEXT NOT NULL,
  event_type TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  FOREIGN KEY(parent_task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE,
  FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_file_locks (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, path TEXT NOT NULL,
  holder_task_id TEXT NOT NULL, holder_agent_id TEXT NOT NULL, status TEXT NOT NULL,
  version_before TEXT NOT NULL, version_after TEXT, acquired_at TEXT NOT NULL,
  expires_at REAL NOT NULL, released_at TEXT,
  FOREIGN KEY(holder_task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
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
CREATE TABLE IF NOT EXISTS extension_packages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, extension_id TEXT NOT NULL, version TEXT NOT NULL,
  name TEXT NOT NULL, manifest TEXT NOT NULL, install_path TEXT NOT NULL,
  digest TEXT NOT NULL, signature_status TEXT NOT NULL DEFAULT 'unsigned',
  enabled INTEGER NOT NULL DEFAULT 0, installed_at TEXT NOT NULL,
  activated_at TEXT, last_error TEXT,
  UNIQUE(extension_id, version)
);
CREATE TABLE IF NOT EXISTS workspace_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, key TEXT NOT NULL,
  agent_id TEXT NOT NULL DEFAULT 'natsume-kokoro-001',
  content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'project', source TEXT NOT NULL DEFAULT 'user',
  namespace TEXT NOT NULL DEFAULT 'project', category TEXT NOT NULL DEFAULT 'decision',
  source_task_id TEXT, tags TEXT NOT NULL DEFAULT '[]', applicable_version TEXT,
  project_signature TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL DEFAULT 0.7,
  last_verified_at TEXT, last_used_at TEXT, use_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
  rejected INTEGER NOT NULL DEFAULT 0, invalidated_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(workspace, namespace, key),
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
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
    finally:
        db.close()


def _schema_version(path: Path) -> int:
    if not path.is_file() or path.stat().st_size == 0:
        return 0
    with closing(sqlite3.connect(path)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
        if not exists:
            return 0
        return int(db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])


def _migration_backup(path: Path, current_version: int) -> Path | None:
    if not path.is_file() or path.stat().st_size == 0 or current_version >= SCHEMA_VERSION:
        return None
    folder = database_backup_directory(path)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"pre-migration-v{current_version}-to-v{SCHEMA_VERSION}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
    return target


def _restore_migration_backup(path: Path, backup: Path) -> None:
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(path)) as destination:
        source.backup(destination)


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


def _migration_v9(db: sqlite3.Connection) -> None:
    task_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_tasks)")}
    for column, definition in {
        "input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "output_tokens": "INTEGER NOT NULL DEFAULT 0",
        "phase_tokens": "TEXT NOT NULL DEFAULT '{}'",
        "estimated_cost_usd": "REAL NOT NULL DEFAULT 0",
        "model_route": "TEXT NOT NULL DEFAULT '{}'",
        "cache_hits": "INTEGER NOT NULL DEFAULT 0",
        "cache_misses": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if column not in task_columns:
            db.execute(f"ALTER TABLE agent_tasks ADD COLUMN {column} {definition}")
    run_columns = {row[1] for row in db.execute("PRAGMA table_info(model_runs)")}
    for column, definition in {
        "phase": "TEXT NOT NULL DEFAULT 'analysis'",
        "route_tier": "TEXT NOT NULL DEFAULT 'medium'",
        "task_type": "TEXT NOT NULL DEFAULT 'general'",
        "route_confidence": "REAL NOT NULL DEFAULT 0",
        "max_output_tokens": "INTEGER NOT NULL DEFAULT 0",
        "estimated_cost_usd": "REAL NOT NULL DEFAULT 0",
    }.items():
        if column not in run_columns:
            db.execute(f"ALTER TABLE model_runs ADD COLUMN {column} {definition}")
    db.execute("CREATE INDEX IF NOT EXISTS idx_model_runs_task_phase ON model_runs(task_id, phase, id)")


def _migration_v10(db: sqlite3.Connection) -> None:
    grant_columns = {row[1] for row in db.execute("PRAGMA table_info(approval_grants)")}
    for column, definition in {
        "workspace": "TEXT NOT NULL DEFAULT ''",
        "capabilities": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if column not in grant_columns:
            db.execute(f"ALTER TABLE approval_grants ADD COLUMN {column} {definition}")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS data_flow_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, task_id TEXT,
          source TEXT NOT NULL, sink TEXT NOT NULL, classification TEXT NOT NULL,
          fields TEXT NOT NULL DEFAULT '[]', redactions INTEGER NOT NULL DEFAULT 0,
          allowed INTEGER NOT NULL DEFAULT 1, reason TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS security_snapshots (
          id TEXT PRIMARY KEY, conversation_id INTEGER, task_id TEXT, workspace TEXT NOT NULL,
          reason TEXT NOT NULL, status TEXT NOT NULL, manifest_path TEXT NOT NULL,
          file_count INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0,
          database_backup TEXT, workspace_hash TEXT NOT NULL, created_at TEXT NOT NULL, restored_at TEXT,
          FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_data_flow_events_task ON data_flow_events(task_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_data_flow_events_sink ON data_flow_events(sink, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_security_snapshots_task ON security_snapshots(task_id, created_at DESC);
        """
    )


def _migration_v11(db: sqlite3.Connection) -> None:
    task_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_tasks)")}
    for column, definition in {
        "orchestration_mode": "TEXT NOT NULL DEFAULT 'single'",
        "child_agent_count": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if column not in task_columns:
            db.execute(f"ALTER TABLE agent_tasks ADD COLUMN {column} {definition}")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
          id TEXT PRIMARY KEY, parent_task_id TEXT NOT NULL, parent_agent_id TEXT,
          role TEXT NOT NULL, orchestration_mode TEXT NOT NULL, status TEXT NOT NULL,
          objective TEXT NOT NULL, expected_output TEXT NOT NULL DEFAULT '', output TEXT,
          token_budget INTEGER NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
          tool_allowlist TEXT NOT NULL DEFAULT '[]', file_scope TEXT NOT NULL DEFAULT '[]',
          timeout_seconds INTEGER NOT NULL, risk_level TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 1,
          error TEXT, started_at TEXT NOT NULL, finished_at TEXT,
          FOREIGN KEY(parent_task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE,
          FOREIGN KEY(parent_agent_id) REFERENCES agent_runs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS agent_trace_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, parent_task_id TEXT NOT NULL, agent_run_id TEXT NOT NULL,
          event_type TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          FOREIGN KEY(parent_task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE,
          FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS agent_file_locks (
          id TEXT PRIMARY KEY, workspace TEXT NOT NULL, path TEXT NOT NULL,
          holder_task_id TEXT NOT NULL, holder_agent_id TEXT NOT NULL, status TEXT NOT NULL,
          version_before TEXT NOT NULL, version_after TEXT, acquired_at TEXT NOT NULL,
          expires_at REAL NOT NULL, released_at TEXT,
          FOREIGN KEY(holder_task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_task_id, started_at);
        CREATE INDEX IF NOT EXISTS idx_agent_trace_parent ON agent_trace_events(parent_task_id, id);
        CREATE INDEX IF NOT EXISTS idx_agent_file_locks_task ON agent_file_locks(holder_task_id, acquired_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_file_locks_active
          ON agent_file_locks(workspace, path) WHERE status='active';
        """
    )


def _migration_v12(db: sqlite3.Connection) -> None:
    conversation_columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
    if "agent_profile_id" not in conversation_columns:
        db.execute("ALTER TABLE conversations ADD COLUMN agent_profile_id TEXT NOT NULL DEFAULT 'general'")
    task_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_tasks)")}
    if "agent_profile_id" not in task_columns:
        db.execute("ALTER TABLE agent_tasks ADD COLUMN agent_profile_id TEXT NOT NULL DEFAULT 'general'")
    if "agent_profile_snapshot" not in task_columns:
        db.execute("ALTER TABLE agent_tasks ADD COLUMN agent_profile_snapshot TEXT NOT NULL DEFAULT '{}'")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS extension_packages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, extension_id TEXT NOT NULL, version TEXT NOT NULL,
          name TEXT NOT NULL, manifest TEXT NOT NULL, install_path TEXT NOT NULL,
          digest TEXT NOT NULL, signature_status TEXT NOT NULL DEFAULT 'unsigned',
          enabled INTEGER NOT NULL DEFAULT 0, installed_at TEXT NOT NULL,
          activated_at TEXT, last_error TEXT,
          UNIQUE(extension_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_extension_packages_active
          ON extension_packages(extension_id, enabled, installed_at DESC);
        """
    )


def _migration_v13(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
          event_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, id);
        """
    )


def _migration_v14(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(workspace_memories)")}
    if {"namespace", "category"} <= columns:
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_workspace_memories_namespace "
            "ON workspace_memories(workspace, namespace, category, rejected, confidence, updated_at DESC)"
        )
        return
    db.executescript(
        """
        ALTER TABLE workspace_memories RENAME TO workspace_memories_v13;
        CREATE TABLE workspace_memories (
          id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, key TEXT NOT NULL,
          content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'project', source TEXT NOT NULL DEFAULT 'user',
          namespace TEXT NOT NULL DEFAULT 'project', category TEXT NOT NULL DEFAULT 'decision',
          source_task_id TEXT, tags TEXT NOT NULL DEFAULT '[]', applicable_version TEXT,
          project_signature TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL DEFAULT 0.7,
          last_verified_at TEXT, last_used_at TEXT, use_count INTEGER NOT NULL DEFAULT 0,
          success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
          rejected INTEGER NOT NULL DEFAULT 0, invalidated_reason TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(workspace, namespace, key),
          FOREIGN KEY(source_task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL
        );
        INSERT INTO workspace_memories(
          id, workspace, key, content, kind, source, namespace, category, source_task_id, tags,
          applicable_version, project_signature, confidence, last_verified_at, last_used_at,
          use_count, success_count, failure_count, rejected, invalidated_reason, created_at, updated_at
        )
        SELECT
          id, workspace, key, content, kind, source, 'project',
          CASE
            WHEN kind='experience' THEN 'successful_fix'
            WHEN lower(key) LIKE '%architecture%' OR lower(key) LIKE '%structure%' THEN 'architecture'
            WHEN lower(key) LIKE '%build%' THEN 'build_command'
            WHEN lower(key) LIKE '%test%' THEN 'test_command'
            WHEN lower(key) LIKE '%convention%' OR lower(key) LIKE '%style%' THEN 'coding_convention'
            WHEN lower(key) LIKE '%issue%' OR lower(key) LIKE '%problem%' THEN 'known_issue'
            WHEN lower(key) LIKE '%constraint%' THEN 'user_constraint'
            ELSE 'decision'
          END,
          source_task_id, tags, applicable_version, project_signature, confidence, last_verified_at,
          last_used_at, use_count, success_count, failure_count, rejected, invalidated_reason,
          created_at, updated_at
        FROM workspace_memories_v13;
        DROP TABLE workspace_memories_v13;
        CREATE INDEX idx_workspace_memories_workspace ON workspace_memories(workspace, updated_at DESC);
        CREATE INDEX idx_workspace_memories_retrieval
          ON workspace_memories(workspace, kind, rejected, confidence, updated_at DESC);
        CREATE INDEX idx_workspace_memories_namespace
          ON workspace_memories(workspace, namespace, category, rejected, confidence, updated_at DESC);
        """
    )


def _migration_v15(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS provider_capabilities (
          id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, endpoint_hash TEXT NOT NULL,
          model TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unknown', capabilities TEXT NOT NULL DEFAULT '{}',
          latency_ms INTEGER, sample_count INTEGER NOT NULL DEFAULT 0, success_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, observed_at TEXT NOT NULL, expires_at REAL NOT NULL,
          UNIQUE(provider, endpoint_hash, model)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_capabilities_lookup
          ON provider_capabilities(provider, model, expires_at DESC);
        CREATE INDEX IF NOT EXISTS idx_model_runs_model_recent
          ON model_runs(model, id DESC);
        """
    )


def _migration_v16(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    if "task_id" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN task_id TEXT")
    if "reasoning_content" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN reasoning_content TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, id)")


def _migration_v17(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(model_runs)")}
    for name, definition in (
        ("context_window_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("reserved_output_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("estimated_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("input_estimate", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE model_runs ADD COLUMN {name} {definition}")


def _migration_v18(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS agents (
          agent_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
          active_identity_version INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_versions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, version INTEGER NOT NULL,
          payload TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL,
          actor_id TEXT NOT NULL, administrator_confirmed INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, UNIQUE(agent_id, version),
          FOREIGN KEY(agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_identity_versions_agent ON identity_versions(agent_id, version DESC);
        """
    )
    for table in ("conversations", "agent_tasks", "messages", "model_runs", "audit_logs", "workspace_memories"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "agent_id" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN agent_id TEXT NOT NULL DEFAULT 'natsume-kokoro-001'")


def _migration_v19(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, user_id TEXT,
          memory_type TEXT NOT NULL, title TEXT, content TEXT NOT NULL, normalized_content TEXT NOT NULL,
          source_type TEXT NOT NULL, source_conversation_id TEXT, source_message_id TEXT,
          confidence REAL NOT NULL DEFAULT 0.5, importance REAL NOT NULL DEFAULT 0.5,
          emotional_weight REAL NOT NULL DEFAULT 0.0, access_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, occurred_at TEXT,
          valid_from TEXT, valid_until TEXT, last_accessed_at TEXT,
          status TEXT NOT NULL DEFAULT 'active', supersedes_memory_id TEXT,
          user_confirmed INTEGER NOT NULL DEFAULT 0, is_locked INTEGER NOT NULL DEFAULT 0,
          is_sensitive INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS memory_candidates (
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, user_id TEXT,
          memory_type TEXT NOT NULL, content TEXT NOT NULL, reason TEXT NOT NULL,
          confidence REAL NOT NULL, importance REAL NOT NULL,
          is_sensitive INTEGER NOT NULL DEFAULT 0, source_conversation_id TEXT,
          status TEXT NOT NULL DEFAULT 'pending', decision_reason TEXT,
          created_at TEXT NOT NULL, decided_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_memories_retrieval
          ON memories(agent_id, user_id, status, memory_type, is_locked, importance DESC, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_candidates_status
          ON memory_candidates(agent_id, status, created_at DESC);
        """
    )


def _migration_v20(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS affect_states (
          agent_id TEXT PRIMARY KEY, trait_json TEXT NOT NULL, mood_json TEXT NOT NULL,
          emotion_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS affect_events (
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL, source_conversation_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relationship_states (
          agent_id TEXT NOT NULL, user_id TEXT NOT NULL, state_json TEXT NOT NULL,
          shared_history_count INTEGER NOT NULL DEFAULT 0, last_meaningful_event_id TEXT,
          updated_at TEXT NOT NULL, PRIMARY KEY(agent_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS relationship_events (
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, user_id TEXT NOT NULL,
          importance TEXT NOT NULL, delta_json TEXT NOT NULL, reason TEXT NOT NULL,
          source_conversation_id TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_affect_events_agent ON affect_events(agent_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_relationship_events_pair ON relationship_events(agent_id, user_id, created_at DESC);
        """
    )


def _migration_v21(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS context_assemblies (
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, conversation_id INTEGER, task_id TEXT,
          model TEXT, token_budget INTEGER NOT NULL, estimated_tokens INTEGER NOT NULL,
          layer_summary_json TEXT NOT NULL, memory_ids_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_context_assemblies_conversation
          ON context_assemblies(conversation_id, created_at DESC);
        """
    )


def _migration_v22(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS consolidation_runs (
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, trigger_type TEXT NOT NULL,
          status TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS continuity_snapshots (
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, summary TEXT NOT NULL,
          source_memory_ids_json TEXT NOT NULL, source_task_ids_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_consolidation_runs_agent ON consolidation_runs(agent_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_continuity_snapshots_agent ON continuity_snapshots(agent_id, created_at DESC);
        """
    )


def _migration_v23(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_queue_items (
          id TEXT PRIMARY KEY,
          conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          task_id TEXT REFERENCES agent_tasks(id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK(kind IN ('submit','resume','steer','system')),
          priority TEXT NOT NULL CHECK(priority IN ('now','next','later')),
          priority_value INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('pending','claimed','consumed','cancelled')),
          content TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          target_scope TEXT NOT NULL DEFAULT 'conversation',
          target_agent_id TEXT,
          claimed_at TEXT,
          consumed_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_queue_pending
          ON conversation_queue_items(status, priority_value, created_at);
        CREATE INDEX IF NOT EXISTS idx_conversation_queue_conversation
          ON conversation_queue_items(conversation_id, status, priority_value, created_at);
        CREATE TABLE IF NOT EXISTS task_artifacts (
          id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
          tool_call_id TEXT NOT NULL,
          media_type TEXT NOT NULL,
          path TEXT NOT NULL,
          total_bytes INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_artifacts_task ON task_artifacts(task_id, created_at DESC);
        """
    )


def _migration_v24(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(mcp_servers)")}
    additions = (
        ("health_status", "TEXT NOT NULL DEFAULT 'untested'"),
        ("tool_count", "INTEGER NOT NULL DEFAULT 0"),
        ("tool_names", "TEXT NOT NULL DEFAULT '[]'"),
        ("last_error", "TEXT"),
        ("last_checked_at", "TEXT"),
    )
    for name, definition in additions:
        if name not in columns:
            db.execute(f"ALTER TABLE mcp_servers ADD COLUMN {name} {definition}")


def _migration_v25(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS execution_segments (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
          status TEXT NOT NULL, reason TEXT NOT NULL, phase TEXT NOT NULL,
          completed_steps TEXT NOT NULL DEFAULT '[]', pending_steps TEXT NOT NULL DEFAULT '[]',
          files_modified TEXT NOT NULL DEFAULT '[]', tool_result_refs TEXT NOT NULL DEFAULT '[]',
          context_summary TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,
          model_calls INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
          started_at TEXT NOT NULL, finished_at TEXT,
          UNIQUE(task_id, sequence),
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_execution_segments_task ON execution_segments(task_id, sequence);
        CREATE TABLE IF NOT EXISTS workspace_instruction_snapshots (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workspace TEXT NOT NULL,
          source_path TEXT NOT NULL, scope_path TEXT NOT NULL, priority INTEGER NOT NULL,
          content_hash TEXT NOT NULL, content_chars INTEGER NOT NULL,
          override INTEGER NOT NULL DEFAULT 0, findings TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_instruction_snapshots_task ON workspace_instruction_snapshots(task_id, priority);
        """
    )


def _migration_v26(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS admin_action_grants (
          id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL,
          operation TEXT NOT NULL, target_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
          ui_session_id TEXT NOT NULL, created_at TEXT NOT NULL, expires_at REAL NOT NULL,
          consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_admin_action_grants_active
          ON admin_action_grants(operation, target_id, expires_at, consumed_at);
        CREATE TABLE IF NOT EXISTS task_leases (
          task_id TEXT PRIMARY KEY, owner_instance_id TEXT NOT NULL, owner_pid INTEGER NOT NULL,
          token_hash TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
          acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at REAL NOT NULL,
          released_at TEXT, status TEXT NOT NULL DEFAULT 'active',
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_task_leases_active ON task_leases(status, expires_at);
        CREATE TABLE IF NOT EXISTS managed_processes (
          pid INTEGER PRIMARY KEY, task_id TEXT NOT NULL, owner_pid INTEGER NOT NULL,
          owner_instance_id TEXT NOT NULL, process_identity TEXT NOT NULL,
          command_hash TEXT NOT NULL, started_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'running', stopped_at TEXT,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_managed_processes_task ON managed_processes(task_id, status);
        """
    )


def _migration_v27(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_context_states (
          task_id TEXT PRIMARY KEY, compiler_version INTEGER NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1, state_json TEXT NOT NULL,
          state_fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS task_decision_ledger (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, source TEXT NOT NULL,
          summary TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_task_decision_ledger_task
          ON task_decision_ledger(task_id, created_at);
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
    (9, _migration_v9),
    (10, _migration_v10),
    (11, _migration_v11),
    (12, _migration_v12),
    (13, _migration_v13),
    (14, _migration_v14),
    (15, _migration_v15),
    (16, _migration_v16),
    (17, _migration_v17),
    (18, _migration_v18),
    (19, _migration_v19),
    (20, _migration_v20),
    (21, _migration_v21),
    (22, _migration_v22),
    (23, _migration_v23),
    (24, _migration_v24),
    (25, _migration_v25),
    (26, _migration_v26),
    (27, _migration_v27),
)


def _backfill_pending_task_queue(db: sqlite3.Connection) -> None:
    pending = db.execute(
        "SELECT t.* FROM agent_tasks t LEFT JOIN conversation_queue_items q "
        "ON q.task_id=t.id AND q.kind IN ('submit','resume') AND q.status IN ('pending','claimed') "
        "WHERE t.status='pending' AND q.id IS NULL ORDER BY t.created_at, t.id"
    ).fetchall()
    for task in pending:
        record = dict(task)
        stamp = now_iso()
        payload = {
            "conversation_id": int(record["conversation_id"]),
            "content": str(record.get("prompt") or "继续未完成任务"),
            "task_id": str(record["id"]),
            "orchestration_mode": str(record.get("orchestration_mode") or "single"),
            "agent_count": max(1, int(record.get("child_agent_count") or 1)),
        }
        db.execute(
            "INSERT INTO conversation_queue_items("
            "id,conversation_id,task_id,kind,priority,priority_value,status,content,payload_json,"
            "target_scope,target_agent_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                payload["conversation_id"],
                payload["task_id"],
                "submit",
                "later",
                20,
                "pending",
                payload["content"],
                json.dumps(payload, ensure_ascii=False),
                "conversation",
                None,
                stamp,
                stamp,
            ),
        )


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError):
        return False
    return True


def _recover_orphaned_tasks(db: sqlite3.Connection) -> None:
    now = time.time()
    for lease in db.execute("SELECT task_id,owner_pid,expires_at FROM task_leases WHERE status='active'").fetchall():
        if float(lease["expires_at"]) <= now or not _pid_is_alive(int(lease["owner_pid"])):
            db.execute(
                "UPDATE task_leases SET status='expired',released_at=?,expires_at=? WHERE task_id=? AND status='active'",
                (now_iso(), now, lease["task_id"]),
            )
    stamp = now_iso()
    db.execute(
        "UPDATE agent_tasks SET status='interrupted', termination_reason='应用上次运行时中断，可从最近检查点继续', "
        "resumable=1, paused_at=?, updated_at=? WHERE status='running' AND NOT EXISTS ("
        "SELECT 1 FROM task_leases l WHERE l.task_id=agent_tasks.id AND l.status='active' AND l.expires_at>?)",
        (stamp, stamp, now),
    )
    db.execute(
        "UPDATE agent_tasks SET status='interrupted', termination_reason=COALESCE(termination_reason,'历史暂停任务已转换为可恢复中断'), "
        "resumable=1, updated_at=? WHERE status='paused'",
        (stamp,),
    )


def init_db() -> None:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed_before = path.is_file()
    backup = _migration_backup(path, _schema_version(path))
    try:
        with connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)", (now_iso(),))
            applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
            for version, migration in MIGRATIONS:
                if version not in applied:
                    migration(db)
                    db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)", (version, now_iso()))
            _recover_orphaned_tasks(db)
            _backfill_pending_task_queue(db)
            db.execute("UPDATE conversations SET permission_mode='ask' WHERE permission_mode IN ('readonly','confirm')")
            db.execute("UPDATE conversations SET permission_mode='full' WHERE permission_mode='auto'")
            db.execute("DELETE FROM approval_grants WHERE expires_at < ?", (time.time(),))
            db.execute("DELETE FROM admin_action_grants WHERE expires_at < ?", (time.time(),))
            db.execute("UPDATE agent_file_locks SET status='expired', released_at=? WHERE status='active' AND expires_at < ?", (now_iso(), time.time()))
            db.execute("DELETE FROM audit_logs WHERE id NOT IN (SELECT id FROM audit_logs ORDER BY id DESC LIMIT 10000)")
    except Exception:
        if backup is not None:
            _restore_migration_backup(path, backup)
        elif not existed_before:
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                candidate.unlink(missing_ok=True)
        raise


def database_status() -> dict[str, Any]:
    with connect() as db:
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        version = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    return {"status": "ok" if integrity == "ok" and version == SCHEMA_VERSION else "error", "integrity": integrity, "schema_version": version, "expected_schema_version": SCHEMA_VERSION}


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]


def audit(conversation_id: int | None, action: str, target: str, status: str, details: Any = None) -> None:
    cleaned, sensitive = redact_payload(details)
    with connect() as db:
        db.execute(
            "INSERT INTO audit_logs(conversation_id, action, target, status, details, created_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, action, target, status, json.dumps(sanitize_details(cleaned), ensure_ascii=False) if details is not None else None, now_iso()),
        )
        if sensitive.redactions:
            flow_conversation = conversation_id if conversation_id is not None and db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone() else None
            db.execute(
                "INSERT INTO data_flow_events(conversation_id, source, sink, classification, fields, redactions, allowed, reason, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (flow_conversation, "application_event", "audit_log", "credential", '["details"]', sensitive.redactions, 1, "credentials redacted before logging", now_iso()),
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
    phase: str = "analysis",
    route_tier: str = "medium",
    task_type: str = "general",
    route_confidence: float = 0.0,
    max_output_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    context_window_tokens: int = 0,
    reserved_output_tokens: int = 0,
    estimated_input_tokens: int = 0,
    input_estimate: bool = False,
) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO model_runs(conversation_id, task_id, provider, model, started_at, finished_at, duration_ms, input_tokens, output_tokens, total_tokens, success, phase, route_tier, task_type, route_confidence, max_output_tokens, estimated_cost_usd, context_window_tokens, reserved_output_tokens, estimated_input_tokens, input_estimate, error_type, retry_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                phase,
                route_tier,
                task_type,
                route_confidence,
                max_output_tokens,
                estimated_cost_usd,
                context_window_tokens,
                reserved_output_tokens,
                estimated_input_tokens,
                int(input_estimate),
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
    if isinstance(value, str):
        cleaned, _ = redact_payload(value)
        return cleaned[:2000] + "…" if len(cleaned) > 2000 else cleaned
    return value


def backup_database() -> dict[str, Any]:
    source = Path(settings.database_path)
    source.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = database_backup_directory(source); backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"agent-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
    with connect() as db, closing(sqlite3.connect(target)) as destination:
        db.backup(destination)
    return {"name": target.name, "size": target.stat().st_size, "created_at": now_iso()}


def database_backups() -> list[dict[str, Any]]:
    folder = database_backup_directory(Path(settings.database_path))
    if not folder.exists(): return []
    paths = [*folder.glob("agent-*.db"), *folder.glob("pre-migration-*.db")]
    return [{"name": path.name, "size": path.stat().st_size, "modified_at": path.stat().st_mtime} for path in sorted(paths, reverse=True)]


def restore_database(name: str) -> dict[str, Any]:
    if Path(name).name != name or not name.endswith(".db") or not name.startswith(("agent-", "pre-migration-")):
        raise ValueError("无效的备份名称")
    source = database_backup_directory(Path(settings.database_path)) / name
    if not source.exists(): raise ValueError("备份不存在")
    safety = backup_database()
    with closing(sqlite3.connect(source)) as backup, connect() as destination:
        backup.backup(destination)
    init_db()
    return {"restored": name, "safety_backup": safety["name"]}
