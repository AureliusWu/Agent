import sqlite3
from pathlib import Path

from app.database import init_db


def test_existing_database_is_migrated_to_runtime_v2(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE agent_tasks (
          id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL, status TEXT NOT NULL,
          prompt TEXT NOT NULL, termination_reason TEXT, model_calls INTEGER NOT NULL DEFAULT 0,
          tool_calls INTEGER NOT NULL DEFAULT 0, files_modified INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE tool_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
          tool TEXT NOT NULL, status TEXT NOT NULL, input TEXT, output TEXT,
          started_at TEXT NOT NULL, finished_at TEXT NOT NULL
        );
        """
    )
    connection.close()
    monkeypatch.setattr("app.database.settings.database_path", database)

    init_db()

    connection = sqlite3.connect(database)
    task_columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_tasks)")}
    tool_columns = {row[1] for row in connection.execute("PRAGMA table_info(tool_runs)")}
    versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    model_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='model_runs'").fetchone()
    verification_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_verifications'").fetchone()
    plan_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_plans'").fetchone()
    repair_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_repair_runs'").fetchone()
    checkpoint_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_checkpoints'").fetchone()
    operation_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_operations'").fetchone()
    working_memory_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_working_memory'").fetchone()
    data_flow_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='data_flow_events'").fetchone()
    snapshot_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='security_snapshots'").fetchone()
    grant_columns = {row[1] for row in connection.execute("PRAGMA table_info(approval_grants)")}
    context_columns = {row[1] for row in connection.execute("PRAGMA table_info(conversation_context)")}
    memory_columns = {row[1] for row in connection.execute("PRAGMA table_info(workspace_memories)")}
    connection.close()

    assert {
        "total_tokens", "input_tokens", "output_tokens", "phase_tokens", "estimated_cost_usd", "model_route",
        "cache_hits", "cache_misses", "current_step", "completed_steps", "pending_steps", "started_at", "finished_at",
        "repair_attempts", "verification_attempts", "current_phase", "checkpoint_sequence", "resume_count", "resumable", "paused_at",
    } <= task_columns
    assert {"task_id", "source", "risk", "confirmed", "duration_ms", "execution_id"} <= tool_columns
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
    assert journal_mode == "wal"
    assert model_table is not None
    assert verification_table is not None
    assert plan_table is not None
    assert repair_table is not None
    assert checkpoint_table is not None
    assert operation_table is not None
    assert working_memory_table is not None
    assert data_flow_table is not None
    assert snapshot_table is not None
    assert {"workspace", "capabilities"} <= grant_columns
    assert "structured_state" in context_columns
    assert {"kind", "source", "tags", "applicable_version", "project_signature", "confidence", "last_verified_at", "use_count", "success_count", "failure_count", "rejected"} <= memory_columns
