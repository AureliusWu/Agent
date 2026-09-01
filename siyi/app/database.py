import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings
from .runtime_paths import database_backup_directory
from app.stt.schemas import DEFAULT_STT_MODEL_ID
from app.database_modules.audit_repository import audit, sanitize_details
from app.database_modules.connection import open_connection, rows
from app.database_modules.migrations import migration_v45
from app.database_modules.recovery_repository import _pid_is_alive, _recover_orphaned_tasks
from app.database_modules.repositories.backup import backup_database, database_backups, restore_database
from app.database_modules.task_repository import record_model_run


SCHEMA_VERSION = 45


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
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  memory_id UNINDEXED, title, content, tags, tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(memory_id,title,content,tags)
  VALUES(new.id,COALESCE(new.title,''),new.content,COALESCE(json_extract(new.metadata_json,'$.tags'),''));
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
  DELETE FROM memories_fts WHERE memory_id=old.id;
  INSERT INTO memories_fts(memory_id,title,content,tags)
  VALUES(new.id,COALESCE(new.title,''),new.content,COALESCE(json_extract(new.metadata_json,'$.tags'),''));
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
  DELETE FROM memories_fts WHERE memory_id=old.id;
END;
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
  title_source TEXT NOT NULL DEFAULT 'fallback', title_locked INTEGER NOT NULL DEFAULT 0,
  title_generated_at TEXT, title_version INTEGER NOT NULL DEFAULT 0, title_input_hash TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_title_jobs (
  id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL, input_hash TEXT NOT NULL, input_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_title_jobs_status ON conversation_title_jobs(status, updated_at);
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
  cached_input_tokens INTEGER NOT NULL DEFAULT 0, uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0, price_snapshot_json TEXT NOT NULL DEFAULT '{}',
  phase_tokens TEXT NOT NULL DEFAULT '{}', estimated_cost_usd REAL NOT NULL DEFAULT 0,
  model_route TEXT NOT NULL DEFAULT '{}', cache_hits INTEGER NOT NULL DEFAULT 0,
  cache_misses INTEGER NOT NULL DEFAULT 0,
  segment_timeout_seconds REAL NOT NULL DEFAULT 0,
  task_deadline_at TEXT,
  token_budget_limit INTEGER NOT NULL DEFAULT 0,
  token_budget_mode TEXT NOT NULL DEFAULT 'soft' CHECK(token_budget_mode IN ('soft','hard')),
  cost_budget_limit REAL,
  orchestration_mode TEXT NOT NULL DEFAULT 'single', child_agent_count INTEGER NOT NULL DEFAULT 0,
  agent_profile_id TEXT NOT NULL DEFAULT 'general',
  agent_profile_snapshot TEXT NOT NULL DEFAULT '{}',
  repair_attempts INTEGER NOT NULL DEFAULT 0, verification_attempts INTEGER NOT NULL DEFAULT 0,
  current_phase TEXT NOT NULL DEFAULT 'analysis', checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
  resume_count INTEGER NOT NULL DEFAULT 0, resumable INTEGER NOT NULL DEFAULT 1, paused_at TEXT,
  current_step TEXT, completed_steps TEXT NOT NULL DEFAULT '[]', pending_steps TEXT NOT NULL DEFAULT '[]',
  lease_generation INTEGER NOT NULL DEFAULT 0,
  provider_profile_snapshot TEXT NOT NULL DEFAULT '{}',
  credential_source TEXT NOT NULL DEFAULT 'missing',
  credential_profile_id TEXT NOT NULL DEFAULT '',
  required_capabilities TEXT NOT NULL DEFAULT '["model"]',
  credential_binding_hash TEXT NOT NULL DEFAULT '',
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
  lease_generation INTEGER NOT NULL DEFAULT 0,
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
  first_token_ms INTEGER,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0, success INTEGER NOT NULL,
  phase TEXT NOT NULL DEFAULT 'analysis', route_tier TEXT NOT NULL DEFAULT 'medium',
  task_type TEXT NOT NULL DEFAULT 'general', route_confidence REAL NOT NULL DEFAULT 0,
  max_output_tokens INTEGER NOT NULL DEFAULT 0, estimated_cost_usd REAL NOT NULL DEFAULT 0,
  context_window_tokens INTEGER NOT NULL DEFAULT 0, reserved_output_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_input_tokens INTEGER NOT NULL DEFAULT 0, input_estimate INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0, uncached_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS permission_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  permission TEXT NOT NULL, effect TEXT NOT NULL CHECK(effect IN ('allow','deny')),
  scope TEXT NOT NULL CHECK(scope IN ('workspace','always')),
  workspace TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '*',
  source TEXT NOT NULL DEFAULT '*', principal TEXT NOT NULL DEFAULT '*',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_permission_policies_lookup
  ON permission_policies(permission, effect, workspace, tool, source, principal, revoked_at);
CREATE TABLE IF NOT EXISTS security_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
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
  lease_generation INTEGER NOT NULL DEFAULT 0,
  UNIQUE(task_id, sequence),
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_operations (
  execution_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
  tool_call_id TEXT NOT NULL, tool TEXT NOT NULL, arguments_hash TEXT NOT NULL,
  status TEXT NOT NULL, result TEXT, side_effect INTEGER NOT NULL DEFAULT 0,
  lease_generation INTEGER NOT NULL DEFAULT 0,
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
  lease_generation INTEGER NOT NULL DEFAULT 0,
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
  claim_owner_instance_id TEXT,
  claim_owner_pid INTEGER,
  claim_generation INTEGER NOT NULL DEFAULT 0,
  claim_expires_at REAL,
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
  filename TEXT NOT NULL DEFAULT '',
  content_sha256 TEXT NOT NULL DEFAULT '',
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
  path TEXT NOT NULL, version TEXT NOT NULL DEFAULT '0.0.0',
  source TEXT NOT NULL DEFAULT 'workspace',
  content_chars INTEGER NOT NULL, content_tokens INTEGER NOT NULL DEFAULT 0,
  trigger_reason TEXT, dependency_chain TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'loaded', error TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  current_step TEXT, trigger_source TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_transitions_task ON task_transitions(task_id, id);
CREATE TABLE IF NOT EXISTS tool_receipts (
  receipt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
  tool_name TEXT NOT NULL, status TEXT NOT NULL, receipt_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tool_receipts_task ON tool_receipts(task_id, created_at);
CREATE TABLE IF NOT EXISTS file_transactions (
  transaction_id TEXT PRIMARY KEY, task_id TEXT, workspace_hash TEXT NOT NULL,
  status TEXT NOT NULL, operation_count INTEGER NOT NULL,
  plan_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_file_transactions_task ON file_transactions(task_id, created_at);
CREATE TABLE IF NOT EXISTS rollback_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL,
  task_id TEXT, change_id TEXT NOT NULL, status TEXT NOT NULL,
  result_json TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(transaction_id) REFERENCES file_transactions(transaction_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_requirements (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, description TEXT NOT NULL,
  requirement_type TEXT NOT NULL, source TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_requirements_task ON task_requirements(task_id, required);
CREATE TABLE IF NOT EXISTS task_acceptance_conditions (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, description TEXT NOT NULL,
  verifier TEXT NOT NULL, evidence_required INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_acceptance_task ON task_acceptance_conditions(task_id);
CREATE TABLE IF NOT EXISTS task_dependencies (
  task_id TEXT NOT NULL, node_id TEXT NOT NULL, depends_on TEXT NOT NULL DEFAULT '[]',
  write_scope TEXT NOT NULL DEFAULT '[]', estimated_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_seconds INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  PRIMARY KEY(task_id, node_id), FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_roles (
  task_id TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL,
  capabilities TEXT NOT NULL DEFAULT '[]', attempt INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL, PRIMARY KEY(task_id, role),
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS role_messages (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sender_role TEXT NOT NULL,
  recipient_role TEXT NOT NULL, message_type TEXT NOT NULL, payload TEXT NOT NULL,
  correlation_id TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_role_messages_task ON role_messages(task_id, created_at);
CREATE TABLE IF NOT EXISTS task_budgets (
  task_id TEXT PRIMARY KEY, total_tokens INTEGER NOT NULL, total_seconds INTEGER NOT NULL,
  model_calls INTEGER NOT NULL, tool_calls INTEGER NOT NULL, allocation TEXT NOT NULL,
  updated_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS performance_traces (
  id TEXT PRIMARY KEY, task_id TEXT, span_name TEXT NOT NULL, component TEXT NOT NULL,
  duration_ms REAL NOT NULL, status TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_performance_traces_task ON performance_traces(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_performance_traces_span ON performance_traces(span_name, started_at);
CREATE TABLE IF NOT EXISTS provider_policies (
  id TEXT PRIMARY KEY, task_id TEXT, preferred_provider TEXT NOT NULL,
  preferred_model TEXT NOT NULL, allow_paid_fallback INTEGER NOT NULL DEFAULT 0,
  fallback_order TEXT NOT NULL DEFAULT '[]', authorization_source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS evaluation_runs (
  id TEXT PRIMARY KEY, suite TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS evaluation_cases (
  run_id TEXT NOT NULL, case_id TEXT NOT NULL, status TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}', reason TEXT, PRIMARY KEY(run_id, case_id),
  FOREIGN KEY(run_id) REFERENCES evaluation_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS release_artifacts (
  id TEXT PRIMARY KEY, version TEXT NOT NULL, artifact_type TEXT NOT NULL,
  path TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
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
    with open_connection(Path(settings.database_path)) as db:
        yield db


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


def _truncate_database_wal(path: Path) -> None:
    """Checkpoint and remove committed WAL pages after a privacy migration."""

    with closing(sqlite3.connect(path, timeout=15)) as db:
        db.execute("PRAGMA busy_timeout = 15000")
        result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if result is None or int(result[0]) != 0:
        raise RuntimeError("database WAL remained busy after privacy migration")


def _safe_database_backup_files(database_path: Path) -> list[Path]:
    """Return only regular, direct children of the managed backup directory."""

    folder = database_backup_directory(database_path)
    if not folder.is_dir() or folder.is_symlink():
        return []
    resolved_folder = folder.resolve()
    candidates: list[Path] = []
    for pattern in ("agent-*.db", "pre-migration-*.db"):
        for candidate in folder.glob(pattern):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                if candidate.resolve().parent != resolved_folder:
                    continue
            except OSError:
                continue
            candidates.append(candidate)
    return sorted(set(candidates))


def _scrub_privacy_sensitive_backup(path: Path) -> None:
    """Remove legacy TTS text and STT local paths from one backup DB.

    Migration backups remain restorable because v41 accepts managed STT
    references and v42 recognises an already-domain-hashed value.  Secure
    delete plus VACUUM removes the old table/index cells from the physical
    backup instead of merely hiding them from SQL queries.
    """

    with closing(sqlite3.connect(path, timeout=15)) as db:
        db.execute("PRAGMA busy_timeout = 15000")
        db.execute("PRAGMA journal_mode = DELETE")
        changed = _scrub_stt_storage_path_rows(db)
        changed += _scrub_tts_idempotency_rows(db)
        db.commit()
        if changed:
            db.execute("VACUUM")
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _scrub_privacy_sensitive_backups(database_path: Path) -> None:
    """Scrub managed migration and user-created backups at every startup."""

    for backup in _safe_database_backup_files(database_path):
        _scrub_privacy_sensitive_backup(backup)


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
          filename TEXT NOT NULL DEFAULT '',
          content_sha256 TEXT NOT NULL DEFAULT '',
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


def _migration_v28(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
    for name, definition in (
        ("title_source", "TEXT NOT NULL DEFAULT 'fallback'"),
        ("title_locked", "INTEGER NOT NULL DEFAULT 0"),
        ("title_generated_at", "TEXT"),
        ("title_version", "INTEGER NOT NULL DEFAULT 0"),
        ("title_input_hash", "TEXT"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE conversations ADD COLUMN {name} {definition}")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_title_jobs (
          id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL UNIQUE,
          status TEXT NOT NULL, input_hash TEXT NOT NULL, input_json TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
          FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_title_jobs_status
          ON conversation_title_jobs(status, updated_at);
        """
    )


def _migration_v29(db: sqlite3.Connection) -> None:
    task_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_tasks)")}
    for name in ("cached_input_tokens", "uncached_input_tokens", "cache_write_tokens"):
        if name not in task_columns:
            db.execute(f"ALTER TABLE agent_tasks ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
    columns = {row[1] for row in db.execute("PRAGMA table_info(model_runs)")}
    for name in ("cached_input_tokens", "uncached_input_tokens", "cache_write_tokens"):
        if name not in columns:
            db.execute(f"ALTER TABLE model_runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
    if "price_snapshot_json" not in columns:
        db.execute("ALTER TABLE model_runs ADD COLUMN price_snapshot_json TEXT NOT NULL DEFAULT '{}'")


def _migration_v30(db: sqlite3.Connection) -> None:
    task_columns = {row[1] for row in db.execute("PRAGMA table_info(agent_tasks)")}
    if "lease_generation" not in task_columns:
        db.execute("ALTER TABLE agent_tasks ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0")
    if "provider_profile_snapshot" not in task_columns:
        db.execute("ALTER TABLE agent_tasks ADD COLUMN provider_profile_snapshot TEXT NOT NULL DEFAULT '{}'")
    for name, definition in (
        ("credential_source", "TEXT NOT NULL DEFAULT 'missing'"),
        ("credential_profile_id", "TEXT NOT NULL DEFAULT ''"),
        ("required_capabilities", "TEXT NOT NULL DEFAULT '[\"model\"]'"),
        ("credential_binding_hash", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in task_columns:
            db.execute(f"ALTER TABLE agent_tasks ADD COLUMN {name} {definition}")
    operation_columns = {row[1] for row in db.execute("PRAGMA table_info(task_operations)")}
    if "lease_generation" not in operation_columns:
        db.execute("ALTER TABLE task_operations ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0")
    for table in ("tool_runs", "task_checkpoints", "task_events"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "lease_generation" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0")
    queue_columns = {row[1] for row in db.execute("PRAGMA table_info(conversation_queue_items)")}
    for name, definition in (
        ("claim_owner_instance_id", "TEXT"),
        ("claim_owner_pid", "INTEGER"),
        ("claim_generation", "INTEGER NOT NULL DEFAULT 0"),
        ("claim_expires_at", "REAL"),
    ):
        if name not in queue_columns:
            db.execute(f"ALTER TABLE conversation_queue_items ADD COLUMN {name} {definition}")


def _migration_v31(db: sqlite3.Connection) -> None:
    # v30 was exercised on the development branch before the full credential
    # and fencing ledger was added. Re-run its idempotent column checks so any
    # such database upgrades safely without rewriting migration history.
    _migration_v30(db)


def _migration_v32(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
          memory_id UNINDEXED, title, content, tags, tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
          INSERT INTO memories_fts(memory_id,title,content,tags)
          VALUES(new.id,COALESCE(new.title,''),new.content,COALESCE(json_extract(new.metadata_json,'$.tags'),''));
        END;
        CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
          DELETE FROM memories_fts WHERE memory_id=old.id;
          INSERT INTO memories_fts(memory_id,title,content,tags)
          VALUES(new.id,COALESCE(new.title,''),new.content,COALESCE(json_extract(new.metadata_json,'$.tags'),''));
        END;
        CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
          DELETE FROM memories_fts WHERE memory_id=old.id;
        END;
        DELETE FROM memories_fts;
        INSERT INTO memories_fts(memory_id,title,content,tags)
        SELECT id,COALESCE(title,''),content,COALESCE(json_extract(metadata_json,'$.tags'),'') FROM memories;
        """
    )


def _migration_v33(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(model_runs)")}
    if "first_token_ms" not in columns:
        db.execute("ALTER TABLE model_runs ADD COLUMN first_token_ms INTEGER")


def _migration_v34(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(skill_runs)")}
    additions = {
        "version": "TEXT NOT NULL DEFAULT '0.0.0'",
        "source": "TEXT NOT NULL DEFAULT 'workspace'",
        "content_tokens": "INTEGER NOT NULL DEFAULT 0",
        "trigger_reason": "TEXT",
        "dependency_chain": "TEXT NOT NULL DEFAULT '[]'",
        "status": "TEXT NOT NULL DEFAULT 'loaded'",
        "error": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE skill_runs ADD COLUMN {name} {declaration}")


def _migration_v35(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS permission_policies (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          permission TEXT NOT NULL, effect TEXT NOT NULL CHECK(effect IN ('allow','deny')),
          scope TEXT NOT NULL CHECK(scope IN ('workspace','always')),
          workspace TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '*',
          source TEXT NOT NULL DEFAULT '*', principal TEXT NOT NULL DEFAULT '*',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revoked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_permission_policies_lookup
          ON permission_policies(permission, effect, workspace, tool, source, principal, revoked_at);
        CREATE TABLE IF NOT EXISTS security_settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )


def _migration_v36(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(task_artifacts)")}
    if "filename" not in columns:
        db.execute(
            "ALTER TABLE task_artifacts ADD COLUMN filename TEXT NOT NULL DEFAULT ''"
        )
    if "content_sha256" not in columns:
        db.execute(
            "ALTER TABLE task_artifacts ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT ''"
        )


def _migration_v37(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_transitions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
          from_status TEXT, to_status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
          current_step TEXT, trigger_source TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_task_transitions_task
          ON task_transitions(task_id, id);
        CREATE TABLE IF NOT EXISTS tool_receipts (
          receipt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
          tool_name TEXT NOT NULL, status TEXT NOT NULL, receipt_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_tool_receipts_task
          ON tool_receipts(task_id, created_at);
        CREATE TABLE IF NOT EXISTS file_transactions (
          transaction_id TEXT PRIMARY KEY, task_id TEXT, workspace_hash TEXT NOT NULL,
          status TEXT NOT NULL, operation_count INTEGER NOT NULL,
          plan_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_file_transactions_task
          ON file_transactions(task_id, created_at);
        CREATE TABLE IF NOT EXISTS rollback_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL,
          task_id TEXT, change_id TEXT NOT NULL, status TEXT NOT NULL,
          result_json TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(transaction_id) REFERENCES file_transactions(transaction_id) ON DELETE CASCADE
        );
        """
    )


def _migration_v38(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_requirements (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, description TEXT NOT NULL,
          requirement_type TEXT NOT NULL, source TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_task_requirements_task ON task_requirements(task_id, required);
        CREATE TABLE IF NOT EXISTS task_acceptance_conditions (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, description TEXT NOT NULL,
          verifier TEXT NOT NULL, evidence_required INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_task_acceptance_task ON task_acceptance_conditions(task_id);
        CREATE TABLE IF NOT EXISTS task_dependencies (
          task_id TEXT NOT NULL, node_id TEXT NOT NULL, depends_on TEXT NOT NULL DEFAULT '[]',
          write_scope TEXT NOT NULL DEFAULT '[]', estimated_tokens INTEGER NOT NULL DEFAULT 0,
          estimated_seconds INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
          PRIMARY KEY(task_id, node_id), FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS task_roles (
          task_id TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL,
          capabilities TEXT NOT NULL DEFAULT '[]', attempt INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL, PRIMARY KEY(task_id, role),
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS role_messages (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sender_role TEXT NOT NULL,
          recipient_role TEXT NOT NULL, message_type TEXT NOT NULL, payload TEXT NOT NULL,
          correlation_id TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_role_messages_task ON role_messages(task_id, created_at);
        CREATE TABLE IF NOT EXISTS task_budgets (
          task_id TEXT PRIMARY KEY, total_tokens INTEGER NOT NULL, total_seconds INTEGER NOT NULL,
          model_calls INTEGER NOT NULL, tool_calls INTEGER NOT NULL, allocation TEXT NOT NULL,
          updated_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS performance_traces (
          id TEXT PRIMARY KEY, task_id TEXT, span_name TEXT NOT NULL, component TEXT NOT NULL,
          duration_ms REAL NOT NULL, status TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
          started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_performance_traces_task ON performance_traces(task_id, started_at);
        CREATE INDEX IF NOT EXISTS idx_performance_traces_span ON performance_traces(span_name, started_at);
        CREATE TABLE IF NOT EXISTS provider_policies (
          id TEXT PRIMARY KEY, task_id TEXT, preferred_provider TEXT NOT NULL,
          preferred_model TEXT NOT NULL, allow_paid_fallback INTEGER NOT NULL DEFAULT 0,
          fallback_order TEXT NOT NULL DEFAULT '[]', authorization_source TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS evaluation_runs (
          id TEXT PRIMARY KEY, suite TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
          summary TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL, finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS evaluation_cases (
          run_id TEXT NOT NULL, case_id TEXT NOT NULL, status TEXT NOT NULL,
          evidence TEXT NOT NULL DEFAULT '{}', reason TEXT, PRIMARY KEY(run_id, case_id),
          FOREIGN KEY(run_id) REFERENCES evaluation_runs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS release_artifacts (
          id TEXT PRIMARY KEY, version TEXT NOT NULL, artifact_type TEXT NOT NULL,
          path TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
          evidence TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        """
    )


def _migration_v39(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ollama_runtime_state (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), mode TEXT NOT NULL,
          status TEXT NOT NULL, pid INTEGER, process_identity TEXT, executable_hash TEXT,
          base_url TEXT NOT NULL, version TEXT, last_error TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS local_model_registry (
          name TEXT PRIMARY KEY, status TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0,
          parameter_size TEXT, quantization TEXT, context_length INTEGER NOT NULL DEFAULT 0,
          size_vram_bytes INTEGER NOT NULL DEFAULT 0, keep_alive TEXT, modified_at TEXT,
          observed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_download_records (
          id TEXT PRIMARY KEY, model TEXT NOT NULL, status TEXT NOT NULL,
          confirmed INTEGER NOT NULL DEFAULT 0, completed_bytes INTEGER NOT NULL DEFAULT 0,
          total_bytes INTEGER NOT NULL DEFAULT 0, error_type TEXT, started_at TEXT NOT NULL,
          finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS model_load_records (
          id TEXT PRIMARY KEY, model TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
          keep_alive TEXT, duration_ms REAL, resources_before TEXT NOT NULL DEFAULT '{}',
          resources_after TEXT NOT NULL DEFAULT '{}', error_type TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resource_samples (
          id TEXT PRIMARY KEY, task_id TEXT, sample_type TEXT NOT NULL,
          values_json TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_resource_samples_time ON resource_samples(created_at);
        CREATE TABLE IF NOT EXISTS tts_settings (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), enabled INTEGER NOT NULL DEFAULT 0,
          provider TEXT NOT NULL DEFAULT 'windows', fallback_provider TEXT NOT NULL DEFAULT 'windows',
          allow_fallback INTEGER NOT NULL DEFAULT 1, voice TEXT NOT NULL DEFAULT '',
          speed REAL NOT NULL DEFAULT 1.0, volume REAL NOT NULL DEFAULT 1.0,
          sample_rate INTEGER NOT NULL DEFAULT 24000, playback_mode TEXT NOT NULL DEFAULT 'MANUAL',
          interrupt_policy TEXT NOT NULL DEFAULT 'IMMEDIATE', cache_enabled INTEGER NOT NULL DEFAULT 1,
          updated_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO tts_settings(singleton,updated_at) VALUES(1, datetime('now'));
        CREATE TABLE IF NOT EXISTS tts_requests (
          request_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, task_id TEXT, message_id TEXT,
          provider TEXT, voice TEXT, status TEXT NOT NULL, cache_id TEXT, sensitive INTEGER NOT NULL DEFAULT 0,
          duration_ms INTEGER NOT NULL DEFAULT 0, synthesis_ms REAL NOT NULL DEFAULT 0,
          error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tts_requests_task ON tts_requests(task_id, created_at);
        CREATE TABLE IF NOT EXISTS tts_cache_entries (
          cache_id TEXT PRIMARY KEY, provider TEXT NOT NULL, model_version TEXT NOT NULL,
          voice TEXT NOT NULL, audio_file TEXT NOT NULL, duration_ms INTEGER NOT NULL DEFAULT 0,
          sample_rate INTEGER NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL,
          last_used_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tts_provider_state (
          provider TEXT PRIMARY KEY, status TEXT NOT NULL, version TEXT,
          metrics_json TEXT NOT NULL DEFAULT '{}', last_error TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audio_playback_records (
          id TEXT PRIMARY KEY, request_id TEXT NOT NULL, task_id TEXT, message_id TEXT,
          status TEXT NOT NULL, started_at TEXT, finished_at TEXT, error_code TEXT,
          FOREIGN KEY(request_id) REFERENCES tts_requests(request_id) ON DELETE CASCADE
        );
        """
    )


def _migration_v40(db: sqlite3.Connection) -> None:
    """Add local voice-input state without persisting recordings or transcripts."""
    script = """
        UPDATE tts_settings
        SET provider='windows', fallback_provider='windows', updated_at=datetime('now')
        WHERE singleton=1 AND provider='melotts';

        CREATE TABLE IF NOT EXISTS microphone_settings (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), selected_device_id TEXT,
          selected_device_label TEXT, max_duration_ms INTEGER NOT NULL DEFAULT 120000,
          min_duration_ms INTEGER NOT NULL DEFAULT 300, auto_send INTEGER NOT NULL DEFAULT 0,
          shortcut TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO microphone_settings(singleton,updated_at) VALUES(1, datetime('now'));

        CREATE TABLE IF NOT EXISTS stt_settings (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), enabled INTEGER NOT NULL DEFAULT 0,
          provider TEXT NOT NULL DEFAULT 'faster_whisper', model_id TEXT NOT NULL DEFAULT '__DEFAULT_STT_MODEL_ID__',
          device TEXT NOT NULL DEFAULT 'cpu', compute_type TEXT NOT NULL DEFAULT 'int8',
          vad INTEGER NOT NULL DEFAULT 1, idle_unload_minutes INTEGER NOT NULL DEFAULT 5,
          gpu_experimental INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO stt_settings(singleton,model_id,updated_at)
        VALUES(1, '__DEFAULT_STT_MODEL_ID__', datetime('now'));

        CREATE TABLE IF NOT EXISTS stt_provider_state (
          provider TEXT PRIMARY KEY, status TEXT NOT NULL, version TEXT,
          metrics_json TEXT NOT NULL DEFAULT '{}', last_error TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stt_models (
          model_id TEXT PRIMARY KEY, provider TEXT NOT NULL, status TEXT NOT NULL,
          size_bytes INTEGER NOT NULL DEFAULT 0, storage_path TEXT NOT NULL,
          device TEXT, compute_type TEXT, loaded_at TEXT, last_used_at TEXT,
          error_code TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stt_download_records (
          id TEXT PRIMARY KEY, model_id TEXT NOT NULL, provider TEXT NOT NULL,
          status TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
          completed_bytes INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0,
          target_directory TEXT NOT NULL, error_code TEXT, started_at TEXT NOT NULL,
          finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS stt_requests (
          request_id TEXT PRIMARY KEY, voice_session_id TEXT, provider TEXT NOT NULL,
          model_id TEXT NOT NULL, status TEXT NOT NULL, audio_sha256 TEXT NOT NULL,
          audio_duration_ms INTEGER NOT NULL DEFAULT 0, transcription_ms REAL NOT NULL DEFAULT 0,
          result_text_hash TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_stt_requests_session ON stt_requests(voice_session_id, created_at);
        CREATE TABLE IF NOT EXISTS stt_metrics (
          id TEXT PRIMARY KEY, request_id TEXT, metric_name TEXT NOT NULL, value REAL,
          unit TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          FOREIGN KEY(request_id) REFERENCES stt_requests(request_id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS voice_sessions (
          voice_session_id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL,
          task_id TEXT, message_id INTEGER, microphone_device_id TEXT,
          recording_started_at TEXT, recording_ended_at TEXT,
          audio_duration_ms INTEGER NOT NULL DEFAULT 0, audio_format TEXT,
          stt_provider TEXT, stt_model TEXT, transcription_text_hash TEXT,
          transcription_duration_ms REAL NOT NULL DEFAULT 0, auto_send INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
          FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE SET NULL,
          FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_voice_sessions_conversation ON voice_sessions(conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_voice_sessions_task ON voice_sessions(task_id, created_at);
        CREATE TABLE IF NOT EXISTS voice_event_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT, voice_session_id TEXT NOT NULL,
          event TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
          FOREIGN KEY(voice_session_id) REFERENCES voice_sessions(voice_session_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_voice_events_session ON voice_event_records(voice_session_id, id);
        """
    db.executescript(script.replace("__DEFAULT_STT_MODEL_ID__", DEFAULT_STT_MODEL_ID))


_STT_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _stt_managed_model_reference(model_id: Any) -> str:
    """Return a stable, non-local-path reference for persisted STT metadata."""
    candidate = str(model_id).strip()
    return f"managed:{candidate}" if _STT_MODEL_ID.fullmatch(candidate) else "managed:unknown"


def _is_absolute_local_path(value: Any) -> bool:
    """Recognize the Windows and POSIX absolute forms old STT rows could hold."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    return candidate.startswith(("/", "\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", candidate))


def _scrub_stt_storage_path_rows(db: sqlite3.Connection) -> int:
    """Replace legacy absolute STT paths on one SQLite connection."""

    db.execute("PRAGMA secure_delete = ON")
    changed = 0
    for table, key_column, column in (
        ("stt_models", "model_id", "storage_path"),
        ("stt_download_records", "id", "target_directory"),
    ):
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            continue
        for record_key, model_id, persisted_value in db.execute(
            f"SELECT {key_column},model_id,{column} FROM {table}"
        ).fetchall():
            if _is_absolute_local_path(persisted_value):
                db.execute(
                    f"UPDATE {table} SET {column}=? WHERE {key_column}=?",
                    (_stt_managed_model_reference(model_id), record_key),
                )
                changed += 1
    return changed


def _migration_v41(db: sqlite3.Connection) -> None:
    """Remove local model paths stored by v40 STT metadata.

    v40 exposed direct local paths to the desktop UI, then accidentally copied
    them into persistent rows.  The UI response remains unchanged, but existing
    databases must be scrubbed during the normal backed-up migration path.
    """

    _scrub_stt_storage_path_rows(db)


_TTS_IDEMPOTENCY_DOMAIN = b"siyi.tts.idempotency.v1\x00"
_TTS_IDEMPOTENCY_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def tts_idempotency_digest(value: Any) -> str:
    """Return the only persistent representation of a renderer idempotency key."""

    encoded = str(value or "").encode("utf-8")
    return "sha256:" + hashlib.sha256(_TTS_IDEMPOTENCY_DOMAIN + encoded).hexdigest()


def _scrub_tts_idempotency_rows(db: sqlite3.Connection) -> int:
    """Replace legacy plaintext TTS idempotency keys on one SQLite connection."""

    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tts_requests'"
    ).fetchone()
    if exists is None:
        return 0
    db.execute("PRAGMA secure_delete = ON")
    changed = 0
    for request_id, value in db.execute(
        "SELECT request_id,idempotency_key FROM tts_requests"
    ).fetchall():
        legacy_key = value if value is not None else f"legacy-request:{request_id}"
        if _TTS_IDEMPOTENCY_DIGEST.fullmatch(str(legacy_key)):
            continue
        db.execute(
            "UPDATE tts_requests SET idempotency_key=? WHERE request_id=?",
            (tts_idempotency_digest(legacy_key), request_id),
        )
        changed += 1
    return changed


def _migration_v42(db: sqlite3.Connection) -> None:
    """Replace legacy plaintext TTS idempotency keys with domain hashes.

    ``secure_delete`` is connection-local, so it must be enabled on the same
    connection that removes the old table and unique-index cells.  ``init_db``
    checkpoints and truncates the WAL after the transaction commits; together
    those steps keep the old value out of both the database freelist and WAL.
    """

    _scrub_tts_idempotency_rows(db)


def _migration_v43(db: sqlite3.Connection) -> None:
    """Add durable task-wide budget and deadline contracts in place.

    Schema 42 tasks did not retain whether their runtime ceiling was an
    explicit hard Token limit.  They therefore migrate to ``soft`` rather than
    guessing a stricter policy and changing the meaning of an in-flight task.
    Existing usage columns remain authoritative and are intentionally not
    copied or reset.
    """

    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(agent_tasks)")}
    additions = {
        "segment_timeout_seconds": "REAL NOT NULL DEFAULT 0",
        "task_deadline_at": "TEXT",
        "token_budget_limit": "INTEGER NOT NULL DEFAULT 0",
        "token_budget_mode": "TEXT NOT NULL DEFAULT 'soft' CHECK(token_budget_mode IN ('soft','hard'))",
        "cost_budget_limit": "REAL",
    }
    for name, definition in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE agent_tasks ADD COLUMN {name} {definition}")


def _migration_v44(db: sqlite3.Connection) -> None:
    """Store MCP credential references only; never copy environment values."""
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(mcp_servers)")}
    if "secret_binding" not in columns:
        db.execute("ALTER TABLE mcp_servers ADD COLUMN secret_binding TEXT")


def _migration_v45(db: sqlite3.Connection) -> None:
    migration_v45(db)


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
    (28, _migration_v28),
    (29, _migration_v29),
    (30, _migration_v30),
    (31, _migration_v31),
    (32, _migration_v32),
    (33, _migration_v33),
    (34, _migration_v34),
    (35, _migration_v35),
    (36, _migration_v36),
    (37, _migration_v37),
    (38, _migration_v38),
    (39, _migration_v39),
    (40, _migration_v40),
    (41, _migration_v41),
    (42, _migration_v42),
    (43, _migration_v43),
    (44, _migration_v44),
    (45, _migration_v45),
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


def init_db() -> None:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed_before = path.is_file()
    backup = _migration_backup(path, _schema_version(path))
    scrub_privacy_wal = False
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
                    scrub_privacy_wal = scrub_privacy_wal or version in (41, 42)
            _recover_orphaned_tasks(db)
            _backfill_pending_task_queue(db)
            db.execute("UPDATE conversations SET permission_mode='ask' WHERE permission_mode='confirm'")
            db.execute("UPDATE conversations SET permission_mode='full' WHERE permission_mode='auto'")
            db.execute("DELETE FROM approval_grants WHERE expires_at < ?", (time.time(),))
            db.execute("DELETE FROM admin_action_grants WHERE expires_at < ?", (time.time(),))
            db.execute("UPDATE agent_file_locks SET status='expired', released_at=? WHERE status='active' AND expires_at < ?", (now_iso(), time.time()))
            db.execute("DELETE FROM audit_logs WHERE id NOT IN (SELECT id FROM audit_logs ORDER BY id DESC LIMIT 10000)")
        if scrub_privacy_wal:
            _truncate_database_wal(path)
    except Exception:
        if backup is not None:
            _restore_migration_backup(path, backup)
        elif not existed_before:
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                candidate.unlink(missing_ok=True)
        raise
    # Pre-migration copies remain recoverable, but they must not become a
    # permanent archive of legacy TTS text or local STT model paths.  Run this
    # on every startup so installations that migrated before this fix are
    # repaired too.
    _scrub_privacy_sensitive_backups(path)


def database_status() -> dict[str, Any]:
    with connect() as db:
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        version = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    return {"status": "ok" if integrity == "ok" and version == SCHEMA_VERSION else "error", "integrity": integrity, "schema_version": version, "expected_schema_version": SCHEMA_VERSION}
