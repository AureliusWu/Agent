import sqlite3
from contextlib import closing

from app import database


def test_schema_43_mcp_rows_upgrade_without_secret_material(tmp_path, monkeypatch):
    path = tmp_path / "mcp-legacy.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(database.SCHEMA)
        db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(1,'legacy')")
        for version, migration in database.MIGRATIONS:
            if version > 43:
                break
            migration(db)
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,'legacy')", (version,))
        db.execute("INSERT INTO mcp_servers(name,transport,url,enabled,created_at) VALUES('old','http','https://mcp.example',0,'legacy')")
    monkeypatch.setattr(database.settings, "database_path", path)
    database.init_db()
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("SELECT name,enabled,secret_binding FROM mcp_servers").fetchone() == ("old", 0, None)
        db.execute("UPDATE mcp_servers SET secret_binding='env:SIYI_MCP_FIXTURE'")
    database.init_db()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT secret_binding FROM mcp_servers").fetchone()[0] == "env:SIYI_MCP_FIXTURE"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert list((tmp_path / "backups").glob("pre-migration-v43-to-*.db"))
