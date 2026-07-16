from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

from . import __version__
from .config import settings
from .database import SCHEMA_VERSION, audit, backup_database, connect, init_db, now_iso
from .identity import AGENT_ID


BACKUP_FORMAT = "siyi-complete-backup-v1"
MAX_BACKUP_BYTES = 256 * 1024 * 1024


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def export_complete_backup(*, include_sensitive: bool, administrator_confirmed: bool) -> tuple[bytes, dict[str, Any]]:
    if not administrator_confirmed:
        raise PermissionError("A complete backup may contain private conversations and memories; explicit administrator confirmation is required")
    with tempfile.TemporaryDirectory(prefix="siyi-backup-") as folder:
        database_path = Path(folder) / "agent.db"
        with connect() as source, closing(sqlite3.connect(database_path)) as destination:
            source.backup(destination)
        database_bytes = database_path.read_bytes()
    autobiography_path = Path(settings.database_path).resolve().parent / "kokoro_autobiography.md"
    autobiography = autobiography_path.read_bytes() if autobiography_path.exists() else b""
    counts: dict[str, int] = {}
    with connect() as db:
        for table in ("identity_versions", "memories", "memory_candidates", "affect_events", "relationship_events", "conversations", "messages"):
            counts[table] = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    manifest = {
        "format": BACKUP_FORMAT,
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "agent_id": AGENT_ID,
        "created_at": now_iso(),
        "contains_sensitive_data": True,
        "warning": "This package may contain private conversations and memories. Store it securely.",
        "counts": counts,
        "files": {
            "agent.db": {"sha256": _sha256(database_bytes), "size": len(database_bytes)},
            "kokoro_autobiography.md": {"sha256": _sha256(autobiography), "size": len(autobiography)},
        },
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("agent.db", database_bytes)
        archive.writestr("kokoro_autobiography.md", autobiography)
    payload = stream.getvalue()
    audit(None, "complete_backup_exported", BACKUP_FORMAT, "ok", {"bytes": len(payload), "contains_sensitive_data": True, "counts": counts})
    return payload, manifest


def inspect_complete_backup(payload: bytes) -> tuple[dict[str, Any], bytes, bytes]:
    if len(payload) > MAX_BACKUP_BYTES:
        raise ValueError("Backup package exceeds 256 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
            if names - {"manifest.json", "agent.db", "kokoro_autobiography.md"}:
                raise ValueError("Backup package contains unexpected files")
            manifest = json.loads(archive.read("manifest.json"))
            database_bytes = archive.read("agent.db")
            autobiography = archive.read("kokoro_autobiography.md") if "kokoro_autobiography.md" in names else b""
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise ValueError("Backup package is invalid") from exc
    if manifest.get("format") != BACKUP_FORMAT or manifest.get("agent_id") != AGENT_ID:
        raise ValueError("Backup format or agent identity does not match")
    if int(manifest.get("schema_version") or 0) > SCHEMA_VERSION:
        raise ValueError("Backup schema is newer than this application")
    files = manifest.get("files") or {}
    if _sha256(database_bytes) != (files.get("agent.db") or {}).get("sha256"):
        raise ValueError("Database backup hash does not match manifest")
    if _sha256(autobiography) != (files.get("kokoro_autobiography.md") or {}).get("sha256"):
        raise ValueError("Continuity file hash does not match manifest")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        handle.write(database_bytes)
        candidate = Path(handle.name)
    try:
        with closing(sqlite3.connect(candidate)) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Backup database integrity check failed")
    finally:
        candidate.unlink(missing_ok=True)
    return manifest, database_bytes, autobiography


def restore_complete_backup(payload: bytes, *, administrator_confirmed: bool) -> dict[str, Any]:
    if not administrator_confirmed:
        raise PermissionError("Restoring a complete backup requires explicit administrator confirmation")
    manifest, database_bytes, autobiography = inspect_complete_backup(payload)
    safety = backup_database()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        handle.write(database_bytes)
        candidate = Path(handle.name)
    try:
        with closing(sqlite3.connect(candidate)) as source, connect() as destination:
            source.backup(destination)
        init_db()
        autobiography_path = Path(settings.database_path).resolve().parent / "kokoro_autobiography.md"
        if autobiography:
            autobiography_path.write_bytes(autobiography)
    except Exception:
        safety_path = Path(settings.database_path).resolve().parent / "backups" / safety["name"]
        with closing(sqlite3.connect(safety_path)) as source, connect() as destination:
            source.backup(destination)
        init_db()
        raise
    finally:
        candidate.unlink(missing_ok=True)
    audit(None, "complete_backup_restored", BACKUP_FORMAT, "ok", {"source_created_at": manifest["created_at"], "safety_backup": safety["name"]})
    return {"restored": True, "format": BACKUP_FORMAT, "source_created_at": manifest["created_at"], "safety_backup": safety["name"]}
