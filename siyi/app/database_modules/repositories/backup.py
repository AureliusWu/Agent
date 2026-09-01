from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from app.runtime_paths import database_backup_directory


def backup_database() -> dict[str, Any]:
    from app import database as facade

    source = Path(facade.settings.database_path)
    source.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = database_backup_directory(source)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"agent-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
    with facade.connect() as db, closing(sqlite3.connect(target)) as destination:
        db.backup(destination)
    return {"name": target.name, "size": target.stat().st_size, "created_at": facade.now_iso()}


def database_backups() -> list[dict[str, Any]]:
    from app import database as facade

    folder = database_backup_directory(Path(facade.settings.database_path))
    if not folder.exists():
        return []
    paths = [*folder.glob("agent-*.db"), *folder.glob("pre-migration-*.db")]
    return [
        {"name": path.name, "size": path.stat().st_size, "modified_at": path.stat().st_mtime}
        for path in sorted(paths, reverse=True)
    ]


def restore_database(name: str) -> dict[str, Any]:
    from app import database as facade

    if Path(name).name != name or not name.endswith(".db") or not name.startswith(("agent-", "pre-migration-")):
        raise ValueError("无效的备份名称")
    source = database_backup_directory(Path(facade.settings.database_path)) / name
    if not source.exists():
        raise ValueError("备份不存在")
    safety = backup_database()
    with closing(sqlite3.connect(source)) as backup, facade.connect() as destination:
        backup.backup(destination)
    facade.init_db()
    return {"restored": name, "safety_backup": safety["name"]}
