from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from app.config import settings
from app.data_flow import record_data_flow
from app.database import connect, now_iso, rows


EXCLUDED_DIRECTORIES = {
    ".agent",
    ".agent-backups",
    ".agent-security-snapshots",
    ".cache",
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".venv",
    "_internal",
    "cache",
    "coverage",
    "venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "__pycache__",
}
SECRET_FILE_PATTERNS = {
    ".env",
    ".env.*",
    ".envrc",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*.key",
    "*.p12",
    "*.pem",
    "*.pfx",
    "*.ppk",
    "application_default_credentials.json",
    "credentials",
    "credentials.json",
    "id_ed25519",
    "id_ed25519.*",
    "id_rsa",
    "id_rsa.*",
    "secret.json",
    "secrets.json",
    "secrets.toml",
    "secrets.yaml",
    "secrets.yml",
    "service-account*.json",
    "token.json",
}
MODEL_FILE_SUFFIXES = {".ckpt", ".gguf", ".onnx", ".pt", ".pth", ".safetensors", ".tflite"}
MODEL_FILE_PATTERNS = {"model*.bin", "pytorch_model*.bin"}

# A few concurrent reads eliminate Windows file-filter latency for workspaces
# containing many tiny files.  Large files still stream directly to the archive
# so snapshot memory stays bounded independently from the configured workspace
# size limit.
_ARCHIVE_READ_WORKERS = 4
_ARCHIVE_BUFFER_FILE_BYTES = 1024 * 1024
_ARCHIVE_BUFFER_BYTES = _ARCHIVE_READ_WORKERS * _ARCHIVE_BUFFER_FILE_BYTES


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


def _is_link_or_junction(path: Path) -> bool:
    """Reject every Windows link-like entry before it can escape the workspace."""
    return path.is_symlink() or path.is_junction()


def _configured_exclusions(value: str) -> set[str]:
    return {item.strip().casefold() for item in value.split(",") if item.strip()}


def _generated_directory_names() -> set[str]:
    # Baseline exclusions are mandatory security/performance boundaries.
    # Configuration adds project-specific generated directories; it cannot
    # re-enable .git, virtual environments, or managed runtime data.
    return {
        *(name.casefold() for name in EXCLUDED_DIRECTORIES),
        *_configured_exclusions(settings.security_snapshot_generated_directory_exclusions),
    }


def _secret_file_patterns() -> set[str]:
    return {
        *(pattern.casefold() for pattern in SECRET_FILE_PATTERNS),
        *_configured_exclusions(settings.security_snapshot_secret_exclusions),
    }


def _is_excluded_relative(relative: PurePosixPath, *, directory: bool = False) -> bool:
    if any(part.casefold() in _generated_directory_names() for part in relative.parts):
        return True
    if directory:
        return False
    lowered = relative.as_posix().casefold()
    name = relative.name.casefold()
    if any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(lowered, pattern) for pattern in _secret_file_patterns()):
        return True
    return relative.suffix.casefold() in MODEL_FILE_SUFFIXES or any(
        fnmatch.fnmatch(name, pattern) for pattern in MODEL_FILE_PATTERNS
    )


def _canonical_snapshot_file(
    root: Path,
    path: Path,
    *,
    parent_prevalidated: bool = False,
) -> Path | None:
    try:
        if _is_link_or_junction(path):
            return None
        candidate = path
        if not parent_prevalidated:
            candidate = path.resolve(strict=True)
            candidate.relative_to(root)
        metadata = os.stat(candidate, follow_symlinks=False)
        return candidate if stat.S_ISREG(metadata.st_mode) else None
    except (OSError, ValueError):
        return None


def _normalize_incremental_paths(root: Path, paths: list[str]) -> list[str]:
    normalized: set[str] = set()
    for value in paths:
        if not value or "\x00" in value:
            raise SnapshotError("Incremental snapshot path is empty or invalid")
        windows_value = value.replace("/", "\\")
        if windows_value.startswith(("\\\\", "\\?\\", "\\.\\")):
            raise SnapshotError("Incremental snapshot path cannot use UNC or device syntax")
        raw = Path(value)
        if raw.drive and not raw.is_absolute():
            raise SnapshotError("Incremental snapshot path cannot be drive-relative")
        candidate = raw if raw.is_absolute() else root / raw
        if candidate.exists() and _is_link_or_junction(candidate):
            raise SnapshotError("Incremental snapshot path cannot be a symlink or junction")
        try:
            resolved = candidate.resolve(strict=False)
            relative = PurePosixPath(resolved.relative_to(root).as_posix())
        except (OSError, ValueError) as error:
            raise SnapshotError("Incremental snapshot path escapes the workspace") from error
        if not relative.parts:
            raise SnapshotError("Incremental snapshot path must name a file")
        if candidate.exists() and candidate.is_dir():
            raise SnapshotError("Incremental snapshot path must name a file")
        if _is_excluded_relative(relative):
            continue
        normalized.add(relative.as_posix())
    return sorted(normalized, key=str.casefold)


def _is_runtime_file(path: Path, database: Path, store: Path) -> bool:
    # ``root`` and the two runtime paths are resolved once before a walk starts.
    # Every traversed child is then lexical beneath that non-linked root, because
    # links and junctions are rejected before this predicate is reached.  Avoid
    # resolving each ordinary file: on Windows that invokes a costly final-path
    # lookup thousands of times for a large snapshot.
    if path in {database, Path(f"{database}-wal"), Path(f"{database}-shm")}:
        return True
    try:
        return path.is_relative_to(store)
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_files(
    root: Path,
    *,
    include_hashes: bool = True,
    paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    total_bytes = 0
    database = Path(settings.database_path).expanduser().resolve()
    store = _store_root().resolve()

    def add_file(
        lexical: Path,
        relative: PurePosixPath,
        *,
        parent_prevalidated: bool = False,
    ) -> None:
        nonlocal total_bytes
        if _is_excluded_relative(relative):
            return
        path = _canonical_snapshot_file(root, lexical, parent_prevalidated=parent_prevalidated)
        if path is None or _is_runtime_file(path, database, store):
            return
        try:
            metadata = path.stat()
        except OSError:
            return
        size = metadata.st_size
        if len(files) + 1 > settings.security_snapshot_max_files:
            raise SnapshotError("Controlled snapshot exceeds the security snapshot file limit")
        if total_bytes + size > settings.security_snapshot_max_bytes:
            raise SnapshotError("Controlled snapshot exceeds the security snapshot size limit")
        total_bytes += size
        item: dict[str, Any] = {
            "path": relative.as_posix(),
            "size": size,
            "mode": stat.S_IMODE(metadata.st_mode),
            "modified_ns": metadata.st_mtime_ns,
        }
        if include_hashes:
            item["sha256"] = _sha256(path)
            item["version_token"] = f"file:{size}:{item['sha256']}"
        files.append(item)
    if paths is not None:
        for relative_text in paths:
            relative = PurePosixPath(relative_text)
            lexical = root / relative
            if not lexical.exists():
                continue
            add_file(lexical, relative)
        return files

    generated_directories = _generated_directory_names()
    for current_text, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_text)
        try:
            current_resolved = current.resolve(strict=True)
            current_relative = PurePosixPath(current_resolved.relative_to(root).as_posix())
        except (OSError, ValueError):
            directory_names[:] = []
            continue
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            child = current / name
            relative = PurePosixPath(*current_relative.parts, name)
            if name.casefold() in generated_directories or _is_excluded_relative(relative, directory=True):
                continue
            if _is_link_or_junction(child):
                continue
            try:
                resolved = child.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if _is_runtime_file(resolved, database, store):
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            relative = PurePosixPath(*current_relative.parts, name)
            # ``current`` was canonicalized and contained at the top of this
            # walk iteration. Avoid another costly Windows final-path lookup
            # for every ordinary file while still rejecting reparse entries.
            add_file(current / name, relative, parent_prevalidated=True)
    return files


def _snapshot_parent_map(root: Path, files: list[dict[str, Any]]) -> dict[Path, Path]:
    """Resolve each unique archive parent once before concurrent file reads."""
    parents: dict[Path, Path] = {}
    for item in files:
        relative = PurePosixPath(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise SnapshotError("Security snapshot contains an unsafe source path")
        lexical_parent = (root / relative).parent
        if lexical_parent in parents:
            continue
        try:
            resolved_parent = lexical_parent.resolve(strict=True)
            resolved_parent.relative_to(root)
        except (OSError, ValueError) as error:
            raise SnapshotError(f"Workspace directory cannot be safely snapshotted: {relative.parent}") from error
        parents[lexical_parent] = resolved_parent
    return parents


def _snapshot_source(
    root: Path,
    item: dict[str, Any],
    parents: dict[Path, Path],
) -> tuple[Path, os.stat_result]:
    relative = str(item["path"])
    lexical = root / PurePosixPath(relative)
    if _is_link_or_junction(lexical):
        raise SnapshotError(f"Symlinked file cannot be safely snapshotted: {relative}")
    try:
        source = parents[lexical.parent] / lexical.name
        before = os.stat(source, follow_symlinks=False)
    except (KeyError, OSError, ValueError) as error:
        raise SnapshotError(f"Workspace file cannot be safely snapshotted: {relative}") from error
    if not stat.S_ISREG(before.st_mode) or before.st_size != int(item["size"]):
        raise SnapshotError(f"Workspace changed while preparing the security snapshot: {relative}")
    return source, before


def _validate_captured_source(item: dict[str, Any], before: os.stat_result, copied: int, after: os.stat_result) -> None:
    if (
        copied != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise SnapshotError(f"Workspace changed while creating the security snapshot: {item['path']}")


def _read_workspace_file(
    root: Path,
    item: dict[str, Any],
    parents: dict[Path, Path],
) -> tuple[bytes, str]:
    """Read a bounded-size entry for the serial ZIP writer without rereading it."""
    source, before = _snapshot_source(root, item, parents)
    try:
        with source.open("rb") as stream:
            payload = stream.read()
        after = source.stat()
    except OSError as error:
        raise SnapshotError(f"Workspace file cannot be safely snapshotted: {item['path']}") from error
    _validate_captured_source(item, before, len(payload), after)
    return payload, hashlib.sha256(payload).hexdigest()


def _stream_workspace_file(
    root: Path,
    item: dict[str, Any],
    archive: zipfile.ZipFile,
    parents: dict[Path, Path],
) -> str:
    """Archive a large entry without retaining its contents in memory."""
    source, before = _snapshot_source(root, item, parents)
    digest = hashlib.sha256()
    copied = 0
    try:
        with source.open("rb") as stream, archive.open(str(item["path"]), "w") as destination:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                destination.write(chunk)
                copied += len(chunk)
        after = source.stat()
    except OSError as error:
        raise SnapshotError(f"Workspace file cannot be safely snapshotted: {item['path']}") from error
    _validate_captured_source(item, before, copied, after)
    return digest.hexdigest()


def _write_workspace_archive(root: Path, archive_path: Path, files: list[dict[str, Any]]) -> None:
    """Write one verified archive stream per workspace file.

    The previous implementation hashed a source file and then asked ZipFile to
    open it again.  On Windows, antivirus/file-system filters make that second
    open dominate a many-small-file snapshot.  Streaming the same bytes through
    the SHA-256 digest and archive retains independent per-file integrity while
    halving source-file opens.
    """
    parents = _snapshot_parent_map(root, files)
    with (
        zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive,
        ThreadPoolExecutor(max_workers=_ARCHIVE_READ_WORKERS, thread_name_prefix="snapshot-read") as executor,
    ):
        pending: dict[int, tuple[Future[tuple[bytes, str]], int]] = {}
        pending_bytes = 0
        next_to_read = 0
        next_to_write = 0

        while next_to_write < len(files):
            # Keep only a bounded amount of small-file content in memory.  The
            # ZIP itself remains serial, preserving deterministic manifest order.
            while next_to_read < len(files) and len(pending) < _ARCHIVE_READ_WORKERS:
                candidate = files[next_to_read]
                candidate_size = int(candidate["size"])
                if candidate_size > _ARCHIVE_BUFFER_FILE_BYTES:
                    break
                if pending and pending_bytes + candidate_size > _ARCHIVE_BUFFER_BYTES:
                    break
                pending[next_to_read] = (
                    executor.submit(_read_workspace_file, root, candidate, parents),
                    candidate_size,
                )
                pending_bytes += candidate_size
                next_to_read += 1

            buffered = pending.pop(next_to_write, None)
            if buffered is not None:
                future, buffered_size = buffered
                pending_bytes -= buffered_size
                payload, digest = future.result()
                archive.writestr(str(files[next_to_write]["path"]), payload)
                files[next_to_write]["sha256"] = digest
                files[next_to_write]["version_token"] = f"file:{files[next_to_write]['size']}:{digest}"
                next_to_write += 1
                continue

            # A large entry deliberately bypasses the in-memory worker queue.
            # No later entry has been submitted, so original archive order holds.
            if next_to_read != next_to_write:
                raise SnapshotError("Security snapshot archive queue lost ordering")
            digest = _stream_workspace_file(root, files[next_to_write], archive, parents)
            files[next_to_write]["sha256"] = digest
            files[next_to_write]["version_token"] = f"file:{files[next_to_write]['size']}:{digest}"
            next_to_read += 1
            next_to_write += 1


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


def _git_state(root: Path, captured_paths: Iterable[str]) -> dict[str, str]:
    if not (root / ".git").exists():
        return {"head": "", "status": "", "working_diff": "", "staged_diff": ""}
    allowed = {path.replace("\\", "/") for path in captured_paths}
    status_lines: list[str] = []
    for line in _git_output(root, ["status", "--short", "--untracked-files=all"]).splitlines():
        if len(line) < 4:
            continue
        reported = line[3:].replace("\\", "/")
        endpoints = {item.strip().strip('"') for item in reported.split(" -> ")}
        if endpoints and endpoints <= allowed:
            status_lines.append(line)
    return {
        "head": _git_output(root, ["rev-parse", "HEAD"], 200).strip(),
        "status": "\n".join(status_lines),
        # Full diffs duplicate archived content and can reintroduce excluded
        # secret files.  The archive + per-file version tokens are the rollback
        # authority; Git metadata is deliberately metadata-only.
        "working_diff": "",
        "staged_diff": "",
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
    paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    root = _workspace_root(workspace)
    requested_paths = _normalize_incremental_paths(root, [str(path) for path in paths]) if paths is not None else None
    snapshot_id = uuid.uuid4().hex
    folder = _store_root() / snapshot_id
    archive_path = folder / "workspace.zip"
    manifest_path = folder / "manifest.json"
    database_backup = folder / "agent.db"
    folder.mkdir(parents=True, exist_ok=False)
    try:
        # Hash while archival bytes are streamed.  The resulting manifest still
        # carries an independent SHA-256 for every entry, without opening each
        # source file a second time.
        files = _collect_files(root, include_hashes=False, paths=requested_paths)
        git = _git_state(root, (str(item["path"]) for item in files))
        # Security snapshots optimize for bounded local recovery latency. The
        # workspace limits already cap disk usage, while per-file SHA-256 keeps
        # integrity independent from ZIP compression. Stored entries avoid the
        # disproportionate deflate overhead of many small source files.
        _write_workspace_archive(root, archive_path, files)
        with connect() as database, closing(sqlite3.connect(database_backup)) as destination:
            database.backup(destination)
        manifest = {
            "version": 2,
            "id": snapshot_id,
            "workspace": str(root),
            "reason": reason,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "created_at": now_iso(),
            "files": files,
            "selection": {
                "mode": "incremental" if requested_paths is not None else "controlled_workspace",
                "requested_paths": requested_paths or [],
                "generated_directory_exclusions": sorted(_generated_directory_names()),
                "secret_exclusion_enabled": True,
                "large_model_exclusion_enabled": True,
            },
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
            "selection_mode": manifest["selection"]["mode"],
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


def _manifest_selection_paths(manifest: dict[str, Any]) -> list[str] | None:
    selection = manifest.get("selection")
    if not isinstance(selection, dict) or selection.get("mode") != "incremental":
        return None
    requested = selection.get("requested_paths")
    if not isinstance(requested, list) or any(not isinstance(item, str) for item in requested):
        raise SnapshotError("Incremental snapshot selection is invalid")
    return requested


def preview_security_snapshot(workspace: str, snapshot_id: str) -> dict[str, Any]:
    record, manifest = _snapshot_record(workspace, snapshot_id)
    root = _workspace_root(workspace)
    selection_paths = _manifest_selection_paths(manifest)
    current = {item["path"]: item for item in _collect_files(root, paths=selection_paths)}
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
        "selection_mode": "incremental" if selection_paths is not None else "controlled_workspace",
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
    selection_paths = _manifest_selection_paths(manifest)
    safety = create_security_snapshot(
        workspace,
        reason=f"before_restore:{snapshot_id}",
        conversation_id=conversation_id,
        task_id=task_id,
        protected_ids={snapshot_id},
        paths=selection_paths,
    )
    archive_path = Path(record["manifest_path"]).parent / "workspace.zip"
    captured = {str(item["path"]): item for item in manifest.get("files") or []}
    # Restoring only needs the current path set to remove files created after a
    # snapshot.  Do not re-hash every current file before the separate safety
    # snapshot performs its integrity capture.
    current = {item["path"]: item for item in _collect_files(root, include_hashes=False, paths=selection_paths)}
    try:
        for relative in sorted(set(current) - set(captured), reverse=True):
            target = (root / PurePosixPath(relative)).resolve(strict=False)
            if not target.is_relative_to(root):
                raise SnapshotError("Restore target escapes the workspace")
            if target.is_file():
                target.unlink()
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(captured):
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
                digest = hashlib.sha256()
                try:
                    # Exclusive creation prevents a workspace-controlled stale
                    # temporary link from being followed during this critical
                    # restore operation.  Hash while extracting rather than
                    # opening the temporary file for a second full read.
                    with archive.open(relative) as source, temporary.open("xb") as destination:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                            destination.write(chunk)
                    if digest.hexdigest() != expected["sha256"]:
                        raise SnapshotError(f"Snapshot checksum mismatch: {relative}")
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
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
            "selection_mode": "incremental" if selection_paths is not None else "controlled_workspace",
            "safety_snapshot_id": safety["id"],
        }
    except Exception:
        raise
