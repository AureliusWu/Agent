from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from app import database
from app.database_modules.file_journal_migration import migration_v46


def _version45(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        db.executescript(database.SCHEMA)
        db.execute("INSERT INTO schema_migrations VALUES(1,'synthetic')")
        for version, migration in database.MIGRATIONS:
            if version > 45:
                break
            migration(db)
            db.execute("INSERT INTO schema_migrations VALUES(?,'synthetic')", (version,))
        db.execute("INSERT INTO file_transactions(transaction_id,workspace_hash,status,operation_count,plan_json,created_at,updated_at) VALUES('legacy-synthetic','hash','committed',1,'[]','synthetic','synthetic')")


def test_v46_migration_preserves_legacy_transaction_and_takes_backup(tmp_path: Path, monkeypatch):
    path = tmp_path / "version45.db"
    _version45(path)
    monkeypatch.setattr(database.settings, "database_path", path)
    database.init_db()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 46
        assert db.execute("SELECT status,journal_version FROM file_transactions WHERE transaction_id='legacy-synthetic'").fetchone() == ("committed", 0)
        assert db.execute("SELECT COUNT(*) FROM file_transaction_steps").fetchone()[0] == 0
    assert len(list((tmp_path / "backups").glob("pre-migration-v45-to-v46-*.db"))) == 1


def test_v46_failed_migration_restores_version45_backup(tmp_path: Path, monkeypatch):
    path = tmp_path / "version45.db"
    _version45(path)
    monkeypatch.setattr(database.settings, "database_path", path)

    def fail_after_ddl(db):
        migration_v46(db)
        raise RuntimeError("synthetic migration interruption")

    monkeypatch.setattr(database, "MIGRATIONS", [(version, fail_after_ddl if version == 46 else migration)
                                               for version, migration in database.MIGRATIONS])
    with pytest.raises(RuntimeError, match="synthetic migration"):
        database.init_db()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 45
        assert db.execute("SELECT status FROM file_transactions WHERE transaction_id='legacy-synthetic'").fetchone()[0] == "committed"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='file_transaction_steps'").fetchone() is None
