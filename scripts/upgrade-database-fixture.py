from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


SENTINEL = "agent-release-upgrade-preserved"


def create_fixture(path: Path, schema_version: int) -> None:
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing fixture: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE release_upgrade_sentinel(value TEXT NOT NULL);
            """
        )
        database.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES(?, 'fixture')",
            ((version,) for version in range(1, schema_version + 1)),
        )
        database.execute("INSERT INTO release_upgrade_sentinel(value) VALUES(?)", (SENTINEL,))


def verify_fixture(path: Path, expected_schema: int) -> dict[str, object]:
    if not path.is_file():
        raise SystemExit(f"Upgraded database is missing: {path}")
    with sqlite3.connect(path) as database:
        integrity = str(database.execute("PRAGMA quick_check").fetchone()[0])
        schema_version = int(database.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
        sentinel = str(database.execute("SELECT value FROM release_upgrade_sentinel").fetchone()[0])
    backup_pattern = f"pre-migration-v*-to-v{expected_schema}-*.db"
    backup_directory = path.parent.parent / "backups" if path.parent.name.lower() == "data" else path.parent / "backups"
    backups = sorted(backup_directory.glob(backup_pattern))
    result = {
        "status": "ok",
        "integrity": integrity,
        "schema_version": schema_version,
        "sentinel_preserved": sentinel == SENTINEL,
        "migration_backup": backups[-1].name if backups else None,
    }
    if integrity != "ok" or schema_version != expected_schema or sentinel != SENTINEL or not backups:
        raise SystemExit(json.dumps(result, ensure_ascii=False))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or verify an installer upgrade database fixture.")
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--schema", type=int, required=True)
    args = parser.parse_args()
    if args.action == "create":
        create_fixture(args.path, args.schema)
        result = {"status": "ok", "created_schema": args.schema, "path": str(args.path)}
    else:
        result = verify_fixture(args.path, args.schema)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
