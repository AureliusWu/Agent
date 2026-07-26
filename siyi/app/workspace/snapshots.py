from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import settings
from app.data_flow import record_data_flow
from app.database import connect, now_iso, rows


EXCLUDED_DIRECTORIES = {
    ".agent-backups",
    ".agent-security-snapshots",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "__pycache__",
}


class SnapshotError(ValueError):
    pass


def _workspace_root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise SnapshotError("Workspace is not a directory")
    return root


def _store_root() -> Path:
    path = Path(settings.database_path).expanduser().resolve().parent / "security-snapshots"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _is_runtime_file(path: Path, database: Path, store: Path) -> bool:
    resolved = path.resolve(strict=False)
    if resolved in {database, Path(f"{database}-wal"), Path(f"{database}-shm")}:
        return True
    try:
        return resolved.is_relative_to(store)
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_files(root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    total_bytes = 0
    database = Path(settings.database_path).expanduser().resolve()
    store = _store_root().resolve()
    for current_text, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_text)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            child = current / name
            if child.is_symlink():
                raise SnapshotError(f"Symlinked directory cannot be safely snapshotted: {child.relative_to(root)}")
            if name in EXCLUDED_DIRECTORIES or _is_runtime_file(child, database, store):
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            path = current / name
            if _is_runtime_file(path, database, store):
                continue
            if path.is_symlink():
                raise SnapshotError(f"Symlinked file cannot be safely snapshotted: {path.relative_to(root)}")
            if not path.is_file():
                continue
            size = path.stat().st_size
            total_bytes += size
            if len(files) + 1 > settings.security_snapshot_max_files:
                raise SnapshotError("Workspace exceeds the security snapshot file limit")
            if total_bytes > settings.security_snapshot_max_bytes:
                raise SnapshotError("Workspace exceeds the security snapshot size limit")
            files.append({
                "path": path.relative_to(root).as_posix(),
                "size": size,
                "sha256": _sha256(path),
                "mode": stat.S_IMODE(path.stat().st_mode),
            })
    return files


def _git_output(root: Path, args: list[str], limit: int = 100_000) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            shell=False,
        )
        return result.stdout[:limit] if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_state(root: Path) -> dict[str, str]:
    if not (root / ".git").exists():
        return {"head": "", "status": "", "working_diff": "", "staged_diff": ""}
    return {
        "head": _git_output(root, ["rev-parse", "HEAD"], 200).strip(),
        "status": _git_output(root, ["status", "--short", "--untracked-files=all"]),
        "working_diff": _git_output(root, ["diff", "--binary"]),
        "staged_diff": _git_output(root, ["diff", "--cached", "--binary"]),
    }


def _task_state(task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    task = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not task:
        return None
    return {
        "task": task[0],
        "plan": rows("SELECT * FROM task_plans WHERE task_id=?", (task_id,)),
        "working_memory": rows("SELECT * FROM task_working_memory WHERE task_id=?", (task_id,)),
        "checkpoint": rows("SELECT * FROM task_checkpoints WHERE task_id=? ORDER BY sequence DESC LIMIT 1", (task_id,)),
    }


def _workspace_hash(files: list[dict[str, Any]], git: dict[str, str]) -> str:
    basis = json.dumps(
        {"files": [(item["path"], item["sha256"]) for item in files], "git_head": git.get("head", "")},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _prune_security_snapshots(workspace: str, protected_ids: set[str]) -> None:
    stale = rows(
        "SELECT id, manifest_path FROM security_snapshots WHERE workspace=? ORDER BY created_at DESC LIMIT 100000 OFFSET ?",
        (workspace, settings.security_snapshot_retention),
    )
    store = _store_root().resolve()
    for item in stale:
        if item["id"] in protected_ids:
            continue
        folder = Path(item["manifest_path"]).resolve(strict=False).parent
        if folder.is_relative_to(store):
            shutil.rmtree(folder, ignore_errors=True)
            with connect() as database:
                database.execute("DELETE FROM security_snapshots WHERE id=?", (item["id"],))


def create_security_snapshot(
    workspace: str,
    *,
    reason: str,
    conversation_id: int | None = None,
    task_id: str | None = None,
    protected_ids: set[str] | None = None,
) -> dict[str, Any]:
    root = _workspace_root(workspace)
    snapshot_id = uuid.uuid4().hex
    folder = _store_root() / snapshot_id
    archive_path = folder / "workspace.zip"
    manifest_path = folder / "manifest.json"
    database_backup = folder / "agent.db"
    folder.mkdir(parents=True, exist_ok=False)
    try:
        files = _collect_files(root)
        git = _git_state(root)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for item in files:
                archive.write(root / PurePosixPath(item["path"]), item["path"])
        with connect() as database, closing(sqlite3.connect(database_backup)) as destination:
            database.backup(destination)
        manifest = {
            "version": 1,
            "id": snapshot_id,
            "workspace": str(root),
            "reason": reason,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "created_at": now_iso(),
            "files": files,
            "git": git,
            "task_state": _task_state(task_id),
            "database_backup": database_backup.name,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        workspace_hash = _workspace_hash(files, git)
        valid_conversation = conversation_id if conversation_id is not None and rows("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)) else None
        valid_task = task_id if task_id is not None and rows("SELECT 1 FROM agent_tasks WHERE id=?", (task_id,)) else None
        with connect() as database:
            database.execute(
                "INSERT INTO security_snapshots(id, conversation_id, task_id, workspace, reason, status, manifest_path, file_count, total_bytes, database_backup, workspace_hash, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    valid_conversation,
                    valid_task,
                    str(root),
                    reason[:500],
                    "ready",
                    str(manifest_path),
                    len(files),
                    sum(int(item["size"]) for item in files),
                    str(database_backup),
                    workspace_hash,
                    manifest["created_at"],
                ),
            )
        record_data_flow(
            source="workspace_and_database",
            sink="local_security_snapshot",
            classification="confidential",
            fields=("workspace_files", "git_state", "task_state", "database_backup"),
            allowed=True,
            reason=reason,
            conversation_id=valid_conversation,
            task_id=valid_task,
        )
        _prune_security_snapshots(str(root), {snapshot_id, *(protected_ids or set())})
        return {
            "id": snapshot_id,
            "status": "ready",
            "reason": reason,
            "file_count": len(files),
            "total_bytes": sum(int(item["size"]) for item in files),
            "workspace_hash": workspace_hash,
            "created_at": manifest["created_at"],
        }
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def list_security_snapshots(workspace: str, task_id: str | None = None) -> list[dict[str, Any]]:
    root = str(_workspace_root(workspace))
    snapshots = (
        rows("SELECT id, task_id, reason, status, file_count, total_bytes, workspace_hash, created_at, restored_at FROM security_snapshots WHERE workspace=? AND task_id=? ORDER BY created_at DESC LIMIT 100", (root, task_id))
        if task_id
        else rows("SELECT id, task_id, reason, status, file_count, total_bytes, workspace_hash, created_at, restored_at FROM security_snapshots WHERE workspace=? ORDER BY created_at DESC LIMIT 100", (root,))
    )
    return snapshots


def _snapshot_record(workspace: str, snapshot_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not snapshot_id or len(snapshot_id) != 32 or any(character not in "0123456789abcdef" for character in snapshot_id):
        raise SnapshotError("Invalid snapshot id")
    root = _workspace_root(workspace)
    matches = rows("SELECT * FROM security_snapshots WHERE id=? AND workspace=? AND status='ready'", (snapshot_id, str(root)))
    if not matches:
        raise SnapshotError("Security snapshot does not exist for this workspace")
    record = matches[0]
    manifest_path = Path(record["manifest_path"]).resolve(strict=True)
    if not manifest_path.is_relative_to(_store_root().resolve()):
        raise SnapshotError("Snapshot manifest is outside the managed store")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("id") != snapshot_id or Path(manifest.get("workspace", "")).resolve() != root:
        raise SnapshotError("Snapshot manifest does not match its database record")
    return record, manifest


def preview_security_snapshot(workspace: str, snapshot_id: str) -> dict[str, Any]:
    record, manifest = _snapshot_record(workspace, snapshot_id)
    root = _workspace_root(workspace)
    current = {item["path"]: item for item in _collect_files(root)}
    captured = {item["path"]: item for item in manifest.get("files") or []}
    added = sorted(set(current) - set(captured))
    deleted = sorted(set(captured) - set(current))
    modified = sorted(path for path in set(current) & set(captured) if current[path]["sha256"] != captured[path]["sha256"])
    return {
        "id": snapshot_id,
        "reason": record["reason"],
        "added_since_snapshot": added[:1000],
        "deleted_since_snapshot": deleted[:1000],
        "modified_since_snapshot": modified[:1000],
        "change_count": len(added) + len(deleted) + len(modified),
        "truncated": any(len(items) > 1000 for items in (added, deleted, modified)),
        "git": manifest.get("git") or {},
    }


def restore_security_snapshot(
    workspace: str,
    snapshot_id: str,
    *,
    conversation_id: int | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    record, manifest = _snapshot_record(workspace, snapshot_id)
    root = _workspace_root(workspace)
    safety = create_security_snapshot(
        workspace,
        reason=f"before_restore:{snapshot_id}",
        conversation_id=conversation_id,
        task_id=task_id,
        protected_ids={snapshot_id},
    )
    archive_path = Path(record["manifest_path"]).parent / "workspace.zip"
    captured = {str(item["path"]): item for item in manifest.get("files") or []}
    current = {item["path"]: item for item in _collect_files(root)}
    try:
        for relative in sorted(set(current) - set(captured), reverse=True):
            target = (root / PurePosixPath(relative)).resolve(strict=False)
            if not target.is_relative_to(root):
                raise SnapshotError("Restore target escapes the workspace")
            if target.is_file():
                target.unlink()
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = set(archive.namelist())
            if names != set(captured):
                raise SnapshotError("Snapshot archive does not match its manifest")
            for relative, expected in captured.items():
                pure = PurePosixPath(relative)
                if pure.is_absolute() or ".." in pure.parts:
                    raise SnapshotError("Snapshot archive contains an unsafe path")
                target = (root / pure).resolve(strict=False)
                if not target.is_relative_to(root):
                    raise SnapshotError("Restore target escapes the workspace")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{snapshot_id[:8]}.tmp")
                with archive.open(relative) as source, temporary.open("wb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                if _sha256(temporary) != expected["sha256"]:
                    temporary.unlink(missing_ok=True)
                    raise SnapshotError(f"Snapshot checksum mismatch: {relative}")
                os.replace(temporary, target)
                try:
                    target.chmod(int(expected.get("mode") or 0o644))
                except OSError:
                    pass
        with connect() as database:
            database.execute("UPDATE security_snapshots SET restored_at=? WHERE id=?", (now_iso(), snapshot_id))
        return {
            "id": snapshot_id,
            "status": "restored",
            "restored_files": len(captured),
            "removed_files": len(set(current) - set(captured)),
            "safety_snapshot_id": safety["id"],
        }
    except Exception:
        raise
