from __future__ import annotations

import asyncio

import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .permissions import PermissionDecision, authorize
from .data_flow import record_data_flow
from app.workspace.snapshots import SnapshotError, create_security_snapshot, list_security_snapshots, preview_security_snapshot, restore_security_snapshot
from app.tools.registry import ToolValidationError, validate_arguments
from app.security.trust import redact_payload
from app.security.policy import command_policy_error
from app.workspace.index import (
    find_definition,
    find_references,
    find_related_tests,
    find_symbol,
    get_call_chain,
    get_repo_map,
    inspect_diagnostics,
    list_module_dependencies,
)


def _resolve_command_executable(command: str) -> str:
    """Resolve bare commands before CreateProcess applies its app-directory precedence."""

    if any(separator in command for separator in ("/", "\\")) or Path(command).is_absolute():
        return command
    if command.casefold() in {"python", "python.exe"} and Path(sys.executable).name.casefold() in {
        "python",
        "python.exe",
    }:
        return sys.executable
    return shutil.which(command) or command


IGNORED_DIRECTORIES = {".git", "node_modules", "dist", "build", "target", "__pycache__", ".venv", "venv", ".agent-backups"}
BLOCKED_COMMANDS = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "bash", "sh", "sudo", "runas", "reg", "reg.exe", "format", "diskpart", "shutdown"}
_change_id_lock = threading.Lock()
_last_change_ns = 0


class SandboxError(ValueError):
    pass


class FileVersionError(SandboxError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def workspace_root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise SandboxError("工作区不存在或不是目录")
    return root


def safe_path(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    if not relative or "\x00" in relative:
        raise SandboxError("路径不能为空或包含空字符")
    normalized = relative.replace("/", "\\")
    if normalized.startswith(("\\\\", "\\?\\", "\\.\\")):
        raise SandboxError("禁止 UNC 或 Windows 设备路径")
    raw = Path(relative)
    if raw.drive and not raw.is_absolute():
        raise SandboxError("禁止盘符相对路径")
    if raw.parts and raw.parts[0].lower() == ".agent-backups":
        raise SandboxError("内部备份目录不能直接操作")
    candidate = (raw if raw.is_absolute() else root / raw).resolve(strict=must_exist)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SandboxError("路径超出已选择的工作区") from exc
    return candidate


def _result(success: bool, data: dict[str, Any] | None = None, *, error_code: str | None = None, error_message: str | None = None, retryable: bool = False, truncated: bool = False, started: float | None = None) -> dict[str, Any]:
    data = data or {}
    status = "ok" if success else "error"
    return {"success": success, "status": status, "data": data, **data, "error_code": error_code, "error_message": error_message, "error": error_message, "retryable": retryable, "truncated": truncated, "metadata": {"duration_ms": round((time.perf_counter() - started) * 1000) if started else 0}}


def _backup_root(root: Path) -> Path:
    path = root / ".agent-backups"
    path.mkdir(exist_ok=True)
    return path


def _file_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "type": None, "size": 0, "sha256": None}
    if path.is_dir():
        return {"exists": True, "type": "directory", "size": 0, "sha256": None}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"exists": True, "type": "file", "size": path.stat().st_size, "sha256": digest}


def file_version_token(path: Path) -> str:
    state = _file_state(path)
    if not state["exists"]:
        return "missing"
    if state["type"] == "directory":
        return f"directory:{path.stat().st_mtime_ns}"
    return f"file:{state['size']}:{state['sha256']}"


def _require_version(arguments: dict[str, Any], field: str, path: Path) -> str:
    expected = arguments.get(field)
    if not isinstance(expected, str) or not expected:
        raise FileVersionError("version_token_required", f"Missing file version token: {field}")
    actual = file_version_token(path)
    if expected != actual:
        raise FileVersionError("version_conflict", f"File changed after it was read: {path.name}")
    return actual


def _manifest_path(root: Path, change_id: str) -> Path:
    if not re.fullmatch(r"\d+-[a-f0-9]{8}", change_id):
        raise SandboxError("无效的变更 ID")
    return _backup_root(root) / change_id / "manifest.json"


def _save_backup(root: Path, operation: str, paths: list[Path], *, task_id: str | None = None, tool_call_id: str | None = None) -> str:
    global _last_change_ns
    with _change_id_lock:
        _last_change_ns = max(time.time_ns(), _last_change_ns + 1)
        change_id = f"{_last_change_ns}-{uuid.uuid4().hex[:8]}"
    folder = _backup_root(root) / change_id
    folder.mkdir()
    entries = []
    for index, path in enumerate(paths):
        existed = path.exists()
        backup = None
        if existed and path.is_file():
            backup = f"{index}.bak"
            shutil.copy2(path, folder / backup)
        elif existed and path.is_dir():
            backup = f"{index}.dir"
            shutil.copytree(path, folder / backup, symlinks=True)
        entries.append({"path": path.relative_to(root).as_posix(), "existed": existed, "backup": backup, "before": _file_state(path)})
    manifest = {"id": change_id, "operation": operation, "task_id": task_id, "tool_call_id": tool_call_id, "created_at": time.time(), "entries": entries}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return change_id


def _finalize_backup(root: Path, change_id: str) -> None:
    path = _manifest_path(root, change_id)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for entry in manifest["entries"]:
        entry["after"] = _file_state(safe_path(root, entry["path"]))
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _discard_backup(root: Path, change_id: str) -> None:
    shutil.rmtree(_manifest_path(root, change_id).parent, ignore_errors=True)


def _rollback_backup(root: Path, change_id: str) -> None:
    folder = _manifest_path(root, change_id).parent
    try:
        _undo_folder(root, folder)
    except Exception:
        _discard_backup(root, change_id)


def _change_folders(root: Path) -> list[Path]:
    backup_root = root / ".agent-backups"
    if not backup_root.is_dir():
        return []
    return sorted(
        (item for item in backup_root.iterdir() if (item / "manifest.json").exists()),
        key=lambda item: int(item.name.split("-", 1)[0]),
        reverse=True,
    )


def _undo_folder(root: Path, folder: Path) -> dict[str, Any]:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    restored = []
    for entry in reversed(manifest["entries"]):
        target = safe_path(root, entry["path"])
        if entry["existed"] and entry["backup"]:
            if target.exists() and target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = folder / entry["backup"]
            if backup.is_dir():
                shutil.copytree(backup, target, symlinks=True)
            else:
                shutil.copy2(backup, target)
        elif target.exists() and target.is_file():
            target.unlink()
        elif target.exists() and target.is_dir():
            shutil.rmtree(target)
        restored.append(entry["path"])
    shutil.rmtree(folder)
    return {"change_id": manifest["id"], "task_id": manifest.get("task_id"), "restored": restored}


def _undo(root: Path, change_id: str | None = None) -> dict[str, Any]:
    folders = sorted(
        _change_folders(root), key=lambda item: int(item.name.split("-", 1)[0]), reverse=True
    )
    if not folders:
        raise SandboxError("没有可撤销的文件操作")
    folder = _manifest_path(root, change_id).parent if change_id else folders[0]
    if not (folder / "manifest.json").exists():
        raise SandboxError("变更不存在或已撤销")
    return _undo_folder(root, folder)


def _undo_task(root: Path, task_id: str) -> dict[str, Any]:
    matches = []
    for folder in _change_folders(root):
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("task_id") == task_id:
            matches.append(folder)
    if not matches:
        raise SandboxError("该任务没有可撤销的文件操作")
    results = [_undo_folder(root, folder) for folder in matches]
    return {"task_id": task_id, "undone": len(results), "changes": results}


def _list_changes(root: Path, task_id: str | None = None) -> list[dict[str, Any]]:
    changes = []
    for folder in _change_folders(root):
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if task_id and manifest.get("task_id") != task_id:
            continue
        changes.append({key: manifest.get(key) for key in ("id", "operation", "task_id", "tool_call_id", "created_at", "entries")})
    return changes[:200]


def verify_task_changes(workspace: str, task_id: str) -> dict[str, Any]:
    root = workspace_root(workspace)
    changes = _list_changes(root, task_id)
    expected_by_path: dict[str, dict[str, Any]] = {}
    for change in changes:
        for entry in change.get("entries") or []:
            expected_by_path.setdefault(entry["path"], entry.get("after") or {})
    checks = []
    for relative, expected in expected_by_path.items():
        actual = _file_state(safe_path(root, relative))
        passed = actual == expected
        checks.append({"kind": "file", "target": relative, "status": "passed" if passed else "failed", "expected": expected, "actual": actual})
    return {"status": "passed" if checks and all(item["status"] == "passed" for item in checks) else ("not_run" if not checks else "failed"), "checks": checks, "change_count": len(changes)}


def recover_file_operation(workspace: str, task_id: str, tool_call_id: str) -> dict[str, Any] | None:
    """Recover a completed file mutation when the process stopped before recording its result."""
    root = workspace_root(workspace)
    for change in _list_changes(root, task_id):
        if change.get("tool_call_id") != tool_call_id:
            continue
        entries = change.get("entries") or []
        if not entries or any(not entry.get("after") for entry in entries):
            return None
        if any(_file_state(safe_path(root, entry["path"])) != entry["after"] for entry in entries):
            return None
        data = {
            "recovered": True,
            "change_id": change["id"],
            "operation": change.get("operation"),
            "paths": [entry["path"] for entry in entries],
        }
        return _result(True, data)
    return None


def _decode_text(raw: bytes, requested: str = "auto") -> tuple[str, str]:
    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise SandboxError("二进制文件不能作为文本读取")
    if requested != "auto":
        return raw.decode(requested), requested
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeError:
            continue
    raise SandboxError("无法识别文件编码")


def _read_text(path: Path, requested: str = "auto") -> tuple[str, str]:
    return _decode_text(path.read_bytes(), requested)


def _diff(path: str, before: str, after: str) -> str:
    return "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))


def _atomic_write(path: Path, content: str, encoding: str) -> None:
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def write_workspace_binary(
    workspace: str,
    relative: str,
    content: bytes,
    *,
    operation: str,
    create_only: bool = False,
    expected_version_token: str | None = None,
    task_id: str | None = None,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    """Atomically write a workspace binary with the normal recoverable backup record."""
    root = workspace_root(workspace)
    path = safe_path(root, relative)
    if not path.parent.is_dir():
        raise SandboxError("Target parent directory does not exist")
    if path.exists() and not path.is_file():
        raise SandboxError("Target path is not a regular file")
    if create_only and path.exists():
        raise FileVersionError("file_exists", "Target file already exists")
    if expected_version_token is not None:
        if not expected_version_token:
            raise FileVersionError("version_token_required", "Missing file version token")
        actual = file_version_token(path)
        if actual != expected_version_token:
            raise FileVersionError("version_conflict", "File changed after it was read")

    before = _file_state(path)
    change_id = _save_backup(
        root,
        operation,
        [path],
        task_id=task_id,
        tool_call_id=tool_call_id,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _finalize_backup(root, change_id)
    except Exception:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
        _rollback_backup(root, change_id)
        raise
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)

    after = _file_state(path)
    return _result(
        True,
        {
            "path": path.relative_to(root).as_posix(),
            "change_id": change_id,
            "version_before": before,
            "version_after": after,
            "total_bytes": int(after["size"]),
            "sha256": str(after["sha256"]),
        },
    )


def write_workspace_binaries(
    workspace: str,
    outputs: dict[str, bytes],
    *,
    operation: str,
    create_only: bool = True,
    task_id: str | None = None,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    """Atomically publish a bounded set of binary outputs under one recovery record."""
    if not outputs:
        raise SandboxError("At least one output file is required")
    root = workspace_root(workspace)
    paths: list[Path] = []
    for relative in outputs:
        path = safe_path(root, relative)
        if path in paths:
            raise SandboxError("Duplicate output path")
        if not path.parent.is_dir():
            raise SandboxError("Target parent directory does not exist")
        if path.exists() and not path.is_file():
            raise SandboxError("Target path is not a regular file")
        if create_only and path.exists():
            raise FileVersionError("file_exists", "Target file already exists")
        paths.append(path)

    change_id = _save_backup(
        root,
        operation,
        paths,
        task_id=task_id,
        tool_call_id=tool_call_id,
    )
    temporary_files: list[tuple[Path, str]] = []
    try:
        for path, content in zip(paths, outputs.values(), strict=True):
            descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_files.append((path, temporary_name))
        for path, temporary_name in temporary_files:
            os.replace(temporary_name, path)
        _finalize_backup(root, change_id)
    except Exception:
        _rollback_backup(root, change_id)
        raise
    finally:
        for _, temporary_name in temporary_files:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    artifacts = [
        {
            "path": path.relative_to(root).as_posix(),
            "total_bytes": int(state["size"]),
            "sha256": str(state["sha256"]),
        }
        for path in paths
        for state in (_file_state(path),)
    ]
    return _result(
        True,
        {
            "paths": [item["path"] for item in artifacts],
            "change_id": change_id,
            "artifacts": artifacts,
            "total_bytes": sum(item["total_bytes"] for item in artifacts),
        },
    )


def _apply_unified_patch(before: str, patch: str) -> str:
    source = before.splitlines()
    output: list[str] = []
    source_index = 0
    lines = patch.splitlines()
    index = 0
    while index < len(lines) and not lines[index].startswith("@@"):
        index += 1
    if index == len(lines):
        raise SandboxError("Patch 不包含 unified diff hunk")
    while index < len(lines):
        header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", lines[index])
        if not header:
            raise SandboxError("Patch hunk 头格式无效")
        old_start = int(header.group(1)) - 1
        if old_start < source_index or old_start > len(source):
            raise SandboxError("Patch 行号超出文件范围")
        output.extend(source[source_index:old_start])
        source_index = old_start
        index += 1
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line == "\\ No newline at end of file":
                index += 1
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                raise SandboxError("Patch hunk 内容格式无效")
            value = line[1:]
            if line[0] in {" ", "-"}:
                if source_index >= len(source) or source[source_index] != value:
                    raise SandboxError("Patch 与当前文件内容不匹配")
                if line[0] == " ":
                    output.append(value)
                source_index += 1
            else:
                output.append(value)
            index += 1
    output.extend(source[source_index:])
    newline = "\r\n" if "\r\n" in before else "\n"
    trailing = newline if before.endswith(("\n", "\r")) else ""
    return newline.join(output) + trailing


def _search(root: Path, base: Path, query: str, pattern: str, *, regex: bool = False, context_lines: int = 0, max_results: int = 200) -> tuple[list[dict[str, Any]], bool]:
    results: list[dict[str, Any]] = []
    lowered = query.lower()
    matcher = re.compile(query, re.IGNORECASE) if regex else None
    for path in base.rglob("*"):
        relative_path = path.relative_to(root)
        managed_worktree = len(relative_path.parts) >= 2 and relative_path.parts[:2] == (".agent", "worktrees")
        if managed_worktree or any(part in IGNORED_DIRECTORIES for part in relative_path.parts) or not path.is_file() or not fnmatch.fnmatch(path.name, pattern):
            continue
        relative = str(relative_path)
        if (matcher.search(path.name) if matcher else lowered in path.name.lower()):
            results.append({"path": relative, "line": None, "text": "文件名匹配"})
        if path.stat().st_size > 1_000_000 or b"\x00" in path.read_bytes()[:4096]:
            continue
        text, _ = _read_text(path)
        text_lines = text.splitlines()
        for number, line in enumerate(text_lines, 1):
            if matcher.search(line) if matcher else lowered in line.lower():
                start, end = max(number - context_lines - 1, 0), min(number + context_lines, len(text_lines))
                results.append({"path": relative, "line": number, "text": line[:300], "context": text_lines[start:end]})
                if len(results) >= max_results:
                    return results, True
    return results, False


def execute_tool(
    workspace: str,
    mode: str,
    tool: str,
    arguments: dict[str, Any],
    approval_tokens: list[str] | None = None,
    *,
    approval_scope: str = "once",
    conversation_id: int | None = None,
    task_id: str | None = None,
    tool_call_id: str | None = None,
    permission_fn: Callable[..., PermissionDecision] = authorize,
) -> dict[str, Any]:
    started = time.perf_counter()
    root = workspace_root(workspace)
    try:
        spec = validate_arguments(tool, arguments)
    except ToolValidationError as exc:
        return _result(False, error_code="invalid_arguments", error_message=str(exc), started=started)
    mode = {"confirm": "ask", "auto": "full", "readonly": "ask"}.get(mode, mode)
    decision = permission_fn(mode=mode, risk=spec.risk, tool=tool, arguments=arguments, conversation_id=conversation_id, task_id=task_id, approval_tokens=approval_tokens, approval_scope=approval_scope, impact=str(arguments.get("path") or arguments.get("source") or arguments.get("command") or "当前工作区"), workspace=workspace)
    if not decision.allowed:
        return decision.confirmation or _result(False, error_code="confirmation_required", error_message="需要确认", started=started)

    try:
        if tool in {"list_files", "list_directory"}:
            path = safe_path(root, str(arguments.get("path", ".")), must_exist=True)
            if not path.is_dir(): raise SandboxError("目标不是目录")
            items = [{"name": item.name, "path": str(item.relative_to(root)), "type": "directory" if item.is_dir() else "file", "size": item.stat().st_size if item.is_file() else None} for item in sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower()))[:500] if item.name != ".agent-backups"]
            return _result(True, {"items": items}, started=started)
        if tool in {"search_files", "search_text"}:
            matches, truncated = _search(
                root,
                safe_path(root, str(arguments.get("path", ".")), must_exist=True),
                str(arguments["query"]).strip(),
                str(arguments.get("glob", "*")),
                regex=bool(arguments.get("regex", False)),
                context_lines=int(arguments.get("context_lines", 0)),
                max_results=int(arguments.get("max_results", 200)),
            )
            return _result(True, {"matches": matches}, truncated=truncated, started=started)
        if tool in {"read_file", "read_file_range"}:
            path = safe_path(root, str(arguments["path"]), must_exist=True)
            if not path.is_file(): raise SandboxError("目标不是文件")
            if path.stat().st_size > 2_000_000: raise SandboxError("文件超过 2 MB，请使用搜索或缩小读取范围")
            text, encoding = _read_text(path, str(arguments.get("encoding", "auto")))
            lines = text.splitlines()
            start, end = max(int(arguments.get("start_line") or 1), 1), min(int(arguments.get("end_line") or len(lines)), len(lines))
            content = "\n".join(lines[start - 1:end]); max_chars = int(arguments.get("max_chars", 40_000)); truncated = len(content) > max_chars
            artifact: dict[str, Any] = {}
            if truncated and task_id and tool_call_id:
                from app.artifacts.store import store_artifact

                artifact = store_artifact(content, task_id=task_id, tool_call_id=tool_call_id)
            return _result(True, {"path": str(path.relative_to(root)), "start_line": start, "end_line": end, "total_lines": len(lines), "file_size": path.stat().st_size, "encoding": encoding, "version_token": file_version_token(path), "content": content[:max_chars], **artifact}, truncated=truncated, started=started)
        if tool in {"file_metadata", "file_info"}:
            path = safe_path(root, str(arguments["path"]), must_exist=True); stat = path.stat()
            encoding = None
            if path.is_file():
                try: _, encoding = _read_text(path)
                except SandboxError: encoding = "binary"
            return _result(True, {"path": str(path.relative_to(root)), "type": "directory" if path.is_dir() else "file", "size": stat.st_size, "modified_at": stat.st_mtime, "encoding": encoding, "sha256": _file_state(path)["sha256"], "version_token": file_version_token(path)}, started=started)
        if tool == "get_repo_map":
            return _result(True, get_repo_map(root), started=started)
        if tool == "find_symbol":
            return _result(True, find_symbol(root, str(arguments["query"]), exact=bool(arguments.get("exact", False)), kind=arguments.get("kind"), max_results=int(arguments.get("max_results", 50))), started=started)
        if tool == "find_definition":
            return _result(True, find_definition(root, str(arguments["symbol"]), max_results=int(arguments.get("max_results", 20))), started=started)
        if tool == "find_references":
            return _result(True, find_references(root, str(arguments["symbol"]), max_results=int(arguments.get("max_results", 100))), started=started)
        if tool == "list_module_dependencies":
            return _result(True, list_module_dependencies(root, arguments.get("path"), max_results=int(arguments.get("max_results", 200))), started=started)
        if tool == "find_related_tests":
            return _result(True, find_related_tests(root, arguments.get("path"), arguments.get("symbol"), max_results=int(arguments.get("max_results", 50))), started=started)
        if tool == "get_call_chain":
            return _result(True, get_call_chain(root, str(arguments["symbol"]), depth=int(arguments.get("depth", 3)), max_results=int(arguments.get("max_results", 100))), started=started)
        if tool == "inspect_diagnostics":
            return _result(True, inspect_diagnostics(root, arguments.get("path"), max_results=int(arguments.get("max_results", 100))), started=started)
        if tool == "list_worktrees":
            from app.workspace.worktrees import list_worktrees

            return _result(True, list_worktrees(workspace), started=started)
        if tool == "create_worktree":
            from app.workspace.worktrees import create_worktree

            return _result(
                True,
                create_worktree(
                    workspace,
                    str(arguments["name"]),
                    ref=str(arguments.get("ref") or "HEAD"),
                    branch=str(arguments["branch"]) if arguments.get("branch") else None,
                ),
                started=started,
            )
        if tool == "remove_worktree":
            from app.workspace.worktrees import remove_worktree

            return _result(True, remove_worktree(workspace, str(arguments["name"]), force=bool(arguments.get("force", False))), started=started)
        if tool in {"file_diff", "view_diff"}:
            path = safe_path(root, str(arguments["path"])); before = _read_text(path)[0] if path.exists() else ""
            diff = _diff(str(arguments["path"]), before, str(arguments["content"]))
            return _result(True, {"path": str(arguments["path"]), "diff": diff[:40_000]}, truncated=len(diff) > 40_000, started=started)
        if tool == "compare_files":
            left = safe_path(root, str(arguments["left"]), must_exist=True); right = safe_path(root, str(arguments["right"]), must_exist=True)
            diff = _diff(f"{arguments['left']}..{arguments['right']}", _read_text(left)[0], _read_text(right)[0])
            return _result(True, {"left": arguments["left"], "right": arguments["right"], "equal": not diff, "diff": diff[:40_000]}, truncated=len(diff) > 40_000, started=started)
        if tool == "list_file_changes":
            return _result(True, {"changes": _list_changes(root, arguments.get("task_id"))}, started=started)
        if tool == "list_security_snapshots":
            return _result(True, {"snapshots": list_security_snapshots(workspace, arguments.get("task_id"))}, started=started)
        if tool == "preview_security_snapshot":
            return _result(True, preview_security_snapshot(workspace, str(arguments["snapshot_id"])), started=started)
        if tool == "restore_security_snapshot":
            return _result(True, restore_security_snapshot(workspace, str(arguments["snapshot_id"]), conversation_id=conversation_id, task_id=task_id), started=started)
        if tool in {"create_file", "write_file", "replace_text", "apply_patch"}:
            path = safe_path(root, str(arguments["path"]));
            if path == root: raise SandboxError("禁止将工作区根目录作为文件目标")
            if path.exists() and not path.is_file(): raise SandboxError("目标不是文件")
            if tool == "create_file" and path.exists(): raise SandboxError("目标文件已存在")
            version_before = "missing" if tool == "create_file" else _require_version(arguments, "expected_version_token", path)
            before, detected_encoding = _read_text(path) if path.exists() else ("", "utf-8")
            encoding = str(arguments.get("encoding", "auto")); encoding = detected_encoding if encoding == "auto" else encoding
            if tool in {"create_file", "write_file"}:
                content = str(arguments["content"])
            elif tool == "replace_text":
                old_text, new_text = str(arguments["old_text"]), str(arguments["new_text"])
                expected = int(arguments.get("expected_count", 1)); actual = before.count(old_text)
                if actual != expected: raise SandboxError(f"预期匹配 {expected} 次，实际匹配 {actual} 次")
                content = before.replace(old_text, new_text, expected)
            else:
                content = _apply_unified_patch(before, str(arguments["patch"]))
            if "\r\n" in before and "\r\n" not in content:
                content = content.replace("\n", "\r\n")
            if len(content.encode(encoding)) > 5_000_000: raise SandboxError("单次写入不能超过 5 MB")
            diff = _diff(str(arguments["path"]), before, content)
            if bool(arguments.get("dry_run", False)):
                return _result(
                    True,
                    {
                        "dry_run": True,
                        "operation": tool,
                        "path": str(path.relative_to(root)),
                        "bytes_before": path.stat().st_size if path.exists() else 0,
                        "bytes_after": len(content.encode(encoding)),
                        "encoding": encoding,
                        "version_before": version_before,
                        "diff": diff[:40_000],
                    },
                    truncated=len(diff) > 40_000,
                    started=started,
                )
            path.parent.mkdir(parents=True, exist_ok=True); change_id = _save_backup(root, tool, [path], task_id=task_id, tool_call_id=tool_call_id)
            try:
                _atomic_write(path, content, encoding)
                _finalize_backup(root, change_id)
            except Exception:
                _rollback_backup(root, change_id)
                raise
            return _result(True, {"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "encoding": encoding, "change_id": change_id, "version_before": version_before, "version_after": file_version_token(path), "diff": diff[:40_000]}, truncated=len(diff) > 40_000, started=started)
        if tool in {"copy_file", "move_file", "rename_file"}:
            source = safe_path(root, str(arguments["source"]), must_exist=True); destination = safe_path(root, str(arguments["destination"])); destination.parent.mkdir(parents=True, exist_ok=True)
            if tool == "copy_file" and not source.is_file(): raise SandboxError("复制工具仅支持单个文件")
            if destination == root: raise SandboxError("禁止将工作区根目录作为目标")
            source_version = _require_version(arguments, "expected_version_token", source)
            destination_version = _require_version(arguments, "expected_destination_version_token", destination)
            if bool(arguments.get("dry_run", False)):
                return _result(
                    True,
                    {
                        "dry_run": True,
                        "operation": tool,
                        "source": str(source.relative_to(root)),
                        "destination": str(destination.relative_to(root)),
                        "version_before": {"source": source_version, "destination": destination_version},
                        "source_state": _file_state(source),
                        "destination_state": _file_state(destination),
                    },
                    started=started,
                )
            change_id = _save_backup(root, tool, [source, destination], task_id=task_id, tool_call_id=tool_call_id)
            try:
                (shutil.copy2 if tool == "copy_file" else shutil.move)(str(source), str(destination)); _finalize_backup(root, change_id)
            except Exception:
                _rollback_backup(root, change_id)
                raise
            return _result(True, {"source": str(arguments["source"]), "destination": str(arguments["destination"]), "change_id": change_id, "version_before": {"source": source_version, "destination": destination_version}, "version_after": {"source": file_version_token(source), "destination": file_version_token(destination)}}, started=started)
        if tool == "create_directory":
            path = safe_path(root, str(arguments["path"]));
            if path == root: raise SandboxError("工作区根目录已存在")
            if path.exists():
                if not path.is_dir(): raise SandboxError("目标已存在且不是目录")
                return _result(True, {"path": str(path.relative_to(root)), "created": False, "change_id": None}, started=started)
            if bool(arguments.get("dry_run", False)):
                return _result(True, {"dry_run": True, "operation": tool, "path": str(path.relative_to(root)), "created": True}, started=started)
            change_id = _save_backup(root, tool, [path], task_id=task_id, tool_call_id=tool_call_id)
            try:
                path.mkdir(parents=True); _finalize_backup(root, change_id)
            except Exception:
                _rollback_backup(root, change_id)
                raise
            return _result(True, {"path": str(path.relative_to(root)), "created": True, "change_id": change_id}, started=started)
        if tool == "delete_file":
            path = safe_path(root, str(arguments["path"]), must_exist=True)
            version_before = _require_version(arguments, "expected_version_token", path)
            if path.is_dir(): raise SandboxError("禁止递归删除目录")
            if path == root: raise SandboxError("禁止删除工作区根目录")
            if bool(arguments.get("dry_run", False)):
                return _result(
                    True,
                    {
                        "dry_run": True,
                        "operation": tool,
                        "path": str(path.relative_to(root)),
                        "version_before": version_before,
                        "state": _file_state(path),
                    },
                    started=started,
                )
            change_id = _save_backup(root, tool, [path], task_id=task_id, tool_call_id=tool_call_id)
            try:
                path.unlink(); _finalize_backup(root, change_id)
            except Exception:
                _rollback_backup(root, change_id)
                raise
            return _result(
                True,
                {
                    "path": str(arguments["path"]),
                    "change_id": change_id,
                    "trash_id": change_id,
                    "trash_path": f".agent-backups/{change_id}",
                    "recoverable": True,
                    "version_before": version_before,
                    "version_after": "missing",
                },
                started=started,
            )
        if tool == "delete_directory":
            path = safe_path(root, str(arguments["path"]), must_exist=True)
            if path == root: raise SandboxError("禁止删除工作区根目录")
            if not path.is_dir(): raise SandboxError("目标不是目录")
            version_before = _require_version(arguments, "expected_version_token", path)
            max_entries = int(arguments.get("max_entries", 1000))
            entry_count = sum(1 for _ in path.rglob("*"))
            if entry_count > max_entries:
                raise SandboxError(f"目录包含 {entry_count} 项，超过本次允许的 {max_entries} 项")
            if bool(arguments.get("dry_run", False)):
                return _result(
                    True,
                    {
                        "dry_run": True,
                        "operation": tool,
                        "path": str(path.relative_to(root)),
                        "entry_count": entry_count,
                        "version_before": version_before,
                    },
                    started=started,
                )
            change_id = _save_backup(root, tool, [path], task_id=task_id, tool_call_id=tool_call_id)
            try:
                shutil.rmtree(path)
                _finalize_backup(root, change_id)
            except Exception:
                _rollback_backup(root, change_id)
                raise
            return _result(
                True,
                {
                    "path": str(arguments["path"]),
                    "entry_count": entry_count,
                    "change_id": change_id,
                    "trash_id": change_id,
                    "trash_path": f".agent-backups/{change_id}",
                    "recoverable": True,
                    "version_before": version_before,
                    "version_after": "missing",
                },
                started=started,
            )
        if tool == "undo_file_change":
            return _result(True, _undo(root, arguments.get("change_id")), started=started)
        if tool == "undo_task_changes":
            return _result(True, _undo_task(root, str(arguments["task_id"])), started=started)
        if tool == "run_command":
            cwd = safe_path(root, str(arguments.get("cwd", ".")), must_exist=True); command = str(arguments["command"]).strip()
            if Path(command).name.lower() in BLOCKED_COMMANDS: raise SandboxError("该命令被安全策略禁止")
            policy_error = command_policy_error(
                command, [str(item) for item in arguments.get("args", [])]
            )
            if policy_error:
                raise SandboxError(policy_error)
            snapshot = create_security_snapshot(workspace, reason=f"before_command:{command}", conversation_id=conversation_id, task_id=task_id)
            _, outbound_sensitive = redact_payload(arguments)
            record_data_flow(source="agent_context", sink="local_process", classification=outbound_sensitive.classification, fields=("command", "args", "cwd"), redactions=outbound_sensitive.redactions, allowed=True, reason="approved local command", conversation_id=conversation_id, task_id=task_id)
            timeout = min(max(int(arguments.get("timeout", 60)), 1), 120)
            executable = _resolve_command_executable(command)
            process = subprocess.run([executable, *[str(item) for item in arguments.get("args", [])]], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, shell=False)
            stdout, stderr = process.stdout[-20_000:], process.stderr[-20_000:]
            _, inbound_sensitive = redact_payload({"stdout": stdout, "stderr": stderr})
            record_data_flow(source="local_process", sink="agent_context", classification=inbound_sensitive.classification, fields=("stdout", "stderr", "exit_code"), redactions=inbound_sensitive.redactions, allowed=True, reason="local command result", conversation_id=conversation_id, task_id=task_id)
            return _result(process.returncode == 0, {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr, "security_snapshot_id": snapshot["id"]}, error_code=None if process.returncode == 0 else "command_failed", error_message=None if process.returncode == 0 else (stderr or f"退出码 {process.returncode}"), retryable=False, truncated=len(process.stdout) > 20_000 or len(process.stderr) > 20_000, started=started)
    except subprocess.TimeoutExpired:
        return _result(False, error_code="tool_timeout", error_message="命令执行超时并已终止", retryable=True, started=started)
    except FileVersionError as exc:
        return _result(False, error_code=exc.code, error_message=str(exc), retryable=exc.code == "version_conflict", started=started)
    except OSError as exc:
        return _result(
            False,
            {
                "io_error": {
                    "type": type(exc).__name__,
                    "errno": exc.errno,
                    "winerror": getattr(exc, "winerror", None),
                    "filename": str(exc.filename or ""),
                }
            },
            error_code="io_error",
            error_message=str(exc),
            retryable=isinstance(exc, (PermissionError, BlockingIOError)),
            started=started,
        )
    except (SandboxError, SnapshotError, KeyError, ValueError) as exc:
        return _result(False, error_code="tool_error", error_message=str(exc), started=started)
    return _result(False, error_code="unknown_tool", error_message=f"未知工具：{tool}", started=started)


def write_uploaded_file(
    workspace: str,
    mode: str,
    path_value: str,
    content: bytes,
    approval_tokens: list[str] | None = None,
    *,
    approval_scope: str = "once",
    conversation_id: int | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    root = workspace_root(workspace)
    arguments = {"path": path_value, "size": len(content)}
    decision = authorize(mode=mode, risk="medium", tool="upload_file", arguments=arguments, conversation_id=conversation_id, task_id=task_id, approval_tokens=approval_tokens, approval_scope=approval_scope, impact=path_value, workspace=workspace)
    if not decision.allowed:
        return decision.confirmation or _result(False, error_code="confirmation_required", error_message="需要确认", started=started)
    path = safe_path(root, path_value)
    if path == root or (path.exists() and not path.is_file()):
        return _result(False, error_code="tool_error", error_message="上传目标必须是工作区内文件", started=started)
    path.parent.mkdir(parents=True, exist_ok=True)
    change_id = _save_backup(root, "upload_file", [path], task_id=task_id)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp_name, path)
        _finalize_backup(root, change_id)
    except Exception as exc:
        if os.path.exists(temp_name): os.unlink(temp_name)
        _rollback_backup(root, change_id)
        return _result(False, error_code="tool_error", error_message=str(exc), started=started)
    return _result(True, {"path": str(path.relative_to(root)), "bytes": len(content), "change_id": change_id}, started=started)


async def execute_command_async(
    workspace: str,
    mode: str,
    arguments: dict[str, Any],
    approval_tokens: list[str] | None = None,
    *,
    approval_scope: str = "once",
    conversation_id: int | None = None,
    task_id: str | None = None,
    permission_fn: Callable[..., PermissionDecision] = authorize,
) -> dict[str, Any]:
    """Run a command without blocking the Agent loop and terminate it on cancellation."""
    started = time.perf_counter()
    root = workspace_root(workspace)
    try:
        spec = validate_arguments("run_command", arguments)
    except ToolValidationError as exc:
        return _result(False, error_code="invalid_arguments", error_message=str(exc), started=started)
    mode = {"confirm": "ask", "auto": "full", "readonly": "ask"}.get(mode, mode)
    decision = permission_fn(mode=mode, risk=spec.risk, tool="run_command", arguments=arguments, conversation_id=conversation_id, task_id=task_id, approval_tokens=approval_tokens, approval_scope=approval_scope, impact=str(arguments.get("command") or "当前工作区"), workspace=workspace)
    if not decision.allowed:
        return decision.confirmation or _result(False, error_code="confirmation_required", error_message="需要确认", started=started)

    try:
        cwd = safe_path(root, str(arguments.get("cwd", ".")), must_exist=True)
        command = str(arguments["command"]).strip()
        if Path(command).name.lower() in BLOCKED_COMMANDS:
            raise SandboxError("该命令被安全策略禁止")
        policy_error = command_policy_error(
            command, [str(item) for item in arguments.get("args", [])]
        )
        if policy_error:
            raise SandboxError(policy_error)
        try:
            snapshot = create_security_snapshot(workspace, reason=f"before_command:{command}", conversation_id=conversation_id, task_id=task_id)
        except SnapshotError as exc:
            return _result(False, error_code="snapshot_failed", error_message=str(exc), started=started)
        _, outbound_sensitive = redact_payload(arguments)
        record_data_flow(source="agent_context", sink="local_process", classification=outbound_sensitive.classification, fields=("command", "args", "cwd"), redactions=outbound_sensitive.redactions, allowed=True, reason="approved local command", conversation_id=conversation_id, task_id=task_id)
        timeout = min(max(int(arguments.get("timeout", 60)), 1), 120)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        executable = _resolve_command_executable(command)
        process = await asyncio.create_subprocess_exec(
            executable,
            *[str(item) for item in arguments.get("args", [])],
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        from .process_supervisor import register_process, terminate_process_tree, unregister_process

        try:
            register_process(process.pid, task_id, executable, [str(item) for item in arguments.get("args", [])], process)
        except Exception:
            terminate_process_tree(process.pid)
            await process.wait()
            raise
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            terminate_process_tree(process.pid)
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                current.uncancel()
            await process.wait()
            unregister_process(process.pid, "cancelled")
            raise
        except TimeoutError:
            terminate_process_tree(process.pid)
            await process.wait()
            unregister_process(process.pid, "timed_out")
            return _result(False, {"security_snapshot_id": snapshot["id"]}, error_code="tool_timeout", error_message="命令执行超时并已终止", retryable=True, started=started)
        unregister_process(process.pid)
        stdout_text = stdout_raw.decode("utf-8", errors="replace")
        stderr_text = stderr_raw.decode("utf-8", errors="replace")
        stdout_clean, _ = redact_payload(stdout_text)
        stderr_clean, _ = redact_payload(stderr_text)
        stdout, stderr = str(stdout_clean)[-20_000:], str(stderr_clean)[-20_000:]
        artifact: dict[str, Any] = {}
        if task_id and len(stdout_raw) + len(stderr_raw) > 40_000:
            from app.artifacts.store import store_json_artifact

            artifact = store_json_artifact(
                {"stdout": stdout_clean, "stderr": stderr_clean, "exit_code": process.returncode},
                task_id=task_id,
                tool_call_id=f"command:{task_id}",
            )
        _, inbound_sensitive = redact_payload({"stdout": stdout, "stderr": stderr})
        record_data_flow(source="local_process", sink="agent_context", classification=inbound_sensitive.classification, fields=("stdout", "stderr", "exit_code"), redactions=inbound_sensitive.redactions, allowed=True, reason="local command result", conversation_id=conversation_id, task_id=task_id)
        return _result(process.returncode == 0, {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr, "security_snapshot_id": snapshot["id"], **artifact}, error_code=None if process.returncode == 0 else "command_failed", error_message=None if process.returncode == 0 else (stderr or f"退出码 {process.returncode}"), retryable=False, truncated=len(stdout_text) > 20_000 or len(stderr_text) > 20_000, started=started)
    except asyncio.CancelledError:
        raise
    except (OSError, SandboxError, SnapshotError, KeyError, ValueError) as exc:
        return _result(False, error_code="tool_error", error_message=str(exc), started=started)


def _is_utf8(data: bytes) -> bool:
    try: data.decode("utf-8"); return True
    except UnicodeDecodeError: return False
