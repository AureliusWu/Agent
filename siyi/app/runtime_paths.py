from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class RuntimeLayout:
    root: Path
    data: Path
    database: Path
    logs: Path
    backups: Path
    user_assets: Path
    artifacts: Path
    workspaces: Path
    cache: Path
    crash: Path
    state: Path
    temp: Path
    extensions: Path
    kokoro: Path

    def directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.data,
            self.logs,
            self.backups,
            self.user_assets,
            self.artifacts,
            self.workspaces,
            self.cache,
            self.crash,
            self.state,
            self.temp,
            self.extensions,
            self.kokoro,
        )


def _local_app_data() -> Path:
    configured = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(configured) if configured else Path.home() / "AppData" / "Local"


def runtime_root(environment: str | None = None) -> Path:
    configured = os.environ.get("AGENT_DATA_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    mode = (environment or os.environ.get("AGENT_RUNTIME_ENV", "development")).strip().lower()
    product = "Agent" if mode in {"production", "release", "desktop"} else "Agent-Dev"
    return _local_app_data() / "AureliusWu" / product


def runtime_layout(environment: str | None = None) -> RuntimeLayout:
    root = runtime_root(environment)
    data = root / "data"
    return RuntimeLayout(
        root=root,
        data=data,
        database=data / "agent.db",
        logs=root / "logs",
        backups=root / "backups",
        user_assets=root / "user-assets",
        artifacts=root / "artifacts",
        workspaces=root / "workspaces",
        cache=root / "cache",
        crash=root / "crash",
        state=root / "state",
        temp=root / "temp",
        extensions=root / "extensions",
        kokoro=root / "kokoro",
    )


def ensure_runtime_layout(layout: RuntimeLayout) -> RuntimeLayout:
    for directory in layout.directories():
        directory.mkdir(parents=True, exist_ok=True)
    return layout


def database_backup_directory(database: Path) -> Path:
    parent = database.parent
    return parent.parent / "backups" if parent.name.lower() == "data" else parent / "backups"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_integrity(path: Path) -> str:
    with closing(sqlite3.connect(path)) as database:
        return str(database.execute("PRAGMA quick_check").fetchone()[0])


def migrate_legacy_root_database(layout: RuntimeLayout) -> dict[str, str] | None:
    """Move the pre-7.0 root database into data/ after a verified backup."""
    legacy = layout.root / "agent.db"
    if layout.database.exists() or not legacy.is_file():
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = layout.backups / f"runtime-layout-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    candidates = [legacy, Path(f"{legacy}-wal"), Path(f"{legacy}-shm")]
    manifest: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "migration": "legacy-root-database-to-data-directory",
        "files": [],
    }
    for source in candidates:
        if not source.is_file():
            continue
        target = backup_dir / source.name
        shutil.copy2(source, target)
        manifest["files"].append(
            {"name": source.name, "size": target.stat().st_size, "sha256": _sha256(target)}
        )
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )

    layout.data.mkdir(parents=True, exist_ok=True)
    temporary = layout.database.with_suffix(".db.migrating")
    temporary.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(legacy)) as source, closing(sqlite3.connect(temporary)) as destination:
            source.backup(destination)
        if _database_integrity(temporary) != "ok":
            raise RuntimeError("migrated database failed SQLite integrity verification")
        os.replace(temporary, layout.database)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    for source in candidates:
        source.unlink(missing_ok=True)
    return {
        "status": "migrated",
        "database": str(layout.database),
        "backup": str(backup_dir),
    }
