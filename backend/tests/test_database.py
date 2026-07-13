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
    model_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='model_runs'").fetchone()
    verification_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_verifications'").fetchone()
    plan_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_plans'").fetchone()
    repair_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_repair_runs'").fetchone()
    connection.close()

    assert {"total_tokens", "current_step", "completed_steps", "pending_steps", "started_at", "finished_at", "repair_attempts", "verification_attempts"} <= task_columns
    assert {"task_id", "source", "risk", "confirmed", "duration_ms"} <= tool_columns
    assert versions == {1, 2, 3, 4, 5, 6}
    assert model_table is not None
    assert verification_table is not None
    assert plan_table is not None
    assert repair_table is not None
