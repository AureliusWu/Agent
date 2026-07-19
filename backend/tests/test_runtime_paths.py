import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.runtime_paths import (
    database_backup_directory,
    ensure_runtime_layout,
    migrate_legacy_root_database,
    runtime_layout,
    runtime_root,
)


def test_runtime_roots_separate_production_and_development(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert runtime_root("production") == tmp_path / "AureliusWu" / "Agent"
    assert runtime_root("development") == tmp_path / "AureliusWu" / "Agent-Dev"


def test_explicit_runtime_root_wins(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "isolated"
    monkeypatch.setenv("AGENT_DATA_ROOT", str(custom))
    assert runtime_layout("production").root == custom


def test_database_backups_use_canonical_runtime_directory(tmp_path: Path) -> None:
    assert database_backup_directory(tmp_path / "data" / "agent.db") == tmp_path / "backups"
    assert database_backup_directory(tmp_path / "isolated.db") == tmp_path / "backups"


def test_legacy_database_migration_is_backed_up_and_verified(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    monkeypatch.setenv("AGENT_DATA_ROOT", str(root))
    layout = ensure_runtime_layout(runtime_layout())
    legacy = root / "agent.db"
    with closing(sqlite3.connect(legacy)) as database:
        database.execute("CREATE TABLE proof(value TEXT NOT NULL)")
        database.execute("INSERT INTO proof VALUES('preserved')")
        database.commit()
    source_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()

    result = migrate_legacy_root_database(layout)

    assert result is not None and result["status"] == "migrated"
    assert not legacy.exists()
    with closing(sqlite3.connect(layout.database)) as database:
        assert database.execute("SELECT value FROM proof").fetchone()[0] == "preserved"
        assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    manifest_path = next(layout.backups.glob("runtime-layout-*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["files"][0]["sha256"] == source_hash
    assert migrate_legacy_root_database(layout) is None
