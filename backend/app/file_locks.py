from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import settings
from .database import connect, now_iso
from .sandbox import safe_path, workspace_root


class FileLockConflict(RuntimeError):
    def __init__(self, paths: Iterable[str], holder_task_id: str) -> None:
        self.paths = tuple(paths)
        self.holder_task_id = holder_task_id
        super().__init__(f"文件正在被任务 {holder_task_id} 修改：{', '.join(self.paths)}")


@dataclass(frozen=True)
class FileLockLease:
    ids: tuple[str, ...]
    workspace: str
    paths: tuple[str, ...]
    holder_task_id: str
    holder_agent_id: str


def mutation_lock_paths(tool: str, arguments: dict[str, Any]) -> tuple[str, ...]:
    if tool in {"run_command", "undo_file_change", "undo_task_changes", "restore_security_snapshot"}:
        return ("*",)
    if tool in {"copy_file"}:
        return tuple(value for value in (str(arguments.get("destination") or ""),) if value)
    if tool in {"move_file", "rename_file"}:
        return tuple(value for value in (str(arguments.get("source") or ""), str(arguments.get("destination") or "")) if value)
    if tool in {"create_file", "write_file", "replace_text", "apply_patch", "create_directory", "delete_file"}:
        value = str(arguments.get("path") or "")
        return (value,) if value else ()
    return ()


def _normalize_paths(workspace: str, paths: Iterable[str]) -> tuple[str, ...]:
    root = workspace_root(workspace)
    normalized: set[str] = set()
    for value in paths:
        if value == "*":
            normalized.add("*")
            continue
        path = safe_path(root, value)
        normalized.add(path.relative_to(root).as_posix() or ".")
    return tuple(sorted(normalized))


def _path_version(workspace: str, relative: str) -> str:
    root = workspace_root(workspace)
    if relative == "*":
        stat = root.stat()
        return f"workspace:{stat.st_mtime_ns}"
    path = safe_path(root, relative)
    if not path.exists():
        return "missing"
    stat = path.stat()
    if path.is_dir():
        return f"directory:{stat.st_mtime_ns}"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"file:{stat.st_size}:{digest.hexdigest()}"


def acquire_file_locks(
    workspace: str,
    paths: Iterable[str],
    *,
    holder_task_id: str,
    holder_agent_id: str,
) -> FileLockLease | None:
    normalized = _normalize_paths(workspace, paths)
    if not normalized:
        return None
    canonical_workspace = str(workspace_root(workspace))
    stamp = now_iso()
    expires_at = time.time() + settings.multi_agent_file_lock_seconds
    lock_ids: list[str] = []
    locked_paths: list[str] = []
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE agent_file_locks SET status='expired', released_at=? WHERE status='active' AND expires_at < ?",
                (stamp, time.time()),
            )
            if "*" in normalized:
                conflicts = list(
                    db.execute(
                        "SELECT path, holder_task_id, holder_agent_id FROM agent_file_locks WHERE workspace=? AND status='active'",
                        (canonical_workspace,),
                    )
                )
            else:
                placeholders = ",".join("?" for _ in normalized)
                conflicts = list(
                    db.execute(
                        f"SELECT path, holder_task_id, holder_agent_id FROM agent_file_locks "
                        f"WHERE workspace=? AND status='active' AND (path='*' OR path IN ({placeholders}))",
                        (canonical_workspace, *normalized),
                    )
                )
            foreign = [row for row in conflicts if row[2] != holder_agent_id]
            if foreign:
                raise FileLockConflict((str(row[0]) for row in foreign), str(foreign[0][1]))
            existing = {str(row[0]): row for row in conflicts}
            for path in normalized:
                if path in existing:
                    continue
                lock_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO agent_file_locks(id, workspace, path, holder_task_id, holder_agent_id, status, version_before, acquired_at, expires_at) "
                    "VALUES(?,?,?,?,?,'active',?,?,?)",
                    (lock_id, canonical_workspace, path, holder_task_id, holder_agent_id, _path_version(canonical_workspace, path), stamp, expires_at),
                )
                lock_ids.append(lock_id)
                locked_paths.append(path)
    except sqlite3.IntegrityError as exc:
        raise FileLockConflict(normalized, "另一个并发任务") from exc
    return FileLockLease(tuple(lock_ids), canonical_workspace, tuple(locked_paths), holder_task_id, holder_agent_id)


def release_file_locks_in_connection(db: sqlite3.Connection, lease: FileLockLease | None, *, status: str = "released") -> None:
    if lease is None or not lease.ids:
        return
    stamp = now_iso()
    versions = [_path_version(lease.workspace, path) for path in lease.paths]
    for lock_id, version in zip(lease.ids, versions, strict=False):
        db.execute(
            "UPDATE agent_file_locks SET status=?, version_after=?, released_at=? "
            "WHERE id=? AND holder_agent_id=? AND status='active'",
            (status, version, stamp, lock_id, lease.holder_agent_id),
        )


def release_file_locks(lease: FileLockLease | None, *, status: str = "released") -> None:
    if lease is None or not lease.ids:
        return
    with connect() as db:
        release_file_locks_in_connection(db, lease, status=status)


def active_file_locks(workspace: str) -> list[dict[str, Any]]:
    canonical_workspace = str(workspace_root(workspace))
    with connect() as db:
        db.execute(
            "UPDATE agent_file_locks SET status='expired', released_at=? WHERE status='active' AND expires_at < ?",
            (now_iso(), time.time()),
        )
        return [dict(row) for row in db.execute(
            "SELECT * FROM agent_file_locks WHERE workspace=? AND status='active' ORDER BY acquired_at",
            (canonical_workspace,),
        )]
