"""Characterization contracts for the incremental database package extraction."""

import importlib
from pathlib import Path

import pytest

from app import database


def test_database_responsibilities_are_separate_modules() -> None:
    for name in (
        "connection", "schema", "migrations", "audit_repository",
        "task_repository", "recovery_repository", "repositories.backup",
    ):
        importlib.import_module(f"app.database_modules.{name}")
    assert database.audit.__module__ == "app.database_modules.audit_repository"
    assert database.record_model_run.__module__ == "app.database_modules.task_repository"
    assert database._recover_orphaned_tasks.__module__ == "app.database_modules.recovery_repository"


def test_database_facade_preserves_connect_rollback_and_clock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(database.settings, "database_path", tmp_path / "characterization.db")
    database.init_db()
    monkeypatch.setattr(database, "now_iso", lambda: "characterization-clock")
    database.audit(None, "test", "metadata-only", "ok", {"content": "private contents"})
    item = database.rows("SELECT * FROM audit_logs WHERE action='test'")[0]
    assert item["created_at"] == "characterization-clock"
    assert "private contents" not in item["details"]
    with pytest.raises(RuntimeError):
        with database.connect() as db:
            db.execute("INSERT INTO agents VALUES('rollback','test',1,'now','now')")
            raise RuntimeError("transaction interrupted")
    assert not database.rows("SELECT * FROM agents WHERE agent_id='rollback'")


def test_repositories_use_facade_connect_seam(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(database.settings, "database_path", tmp_path / "seam.db")
    database.init_db()
    original = database.connect
    calls = []

    def observed_connect():
        calls.append(True)
        return original()

    monkeypatch.setattr(database, "connect", observed_connect)
    database.audit(None, "seam", "test", "ok")
    database.rows("SELECT * FROM audit_logs")
    assert len(calls) == 2
