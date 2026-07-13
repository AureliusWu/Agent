from __future__ import annotations

import difflib
import fnmatch
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .tool_registry import ToolValidationError, requires_confirmation, validate_arguments


IGNORED_DIRECTORIES = {".git", "node_modules", "dist", "build", "target", "__pycache__", ".venv", "venv", ".agent-backups"}
BLOCKED_COMMANDS = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "bash", "sh", "sudo", "runas", "reg", "reg.exe", "format", "diskpart", "shutdown"}


class SandboxError(ValueError):
    pass


def workspace_root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise SandboxError("工作区不存在或不是目录")
    return root


def safe_path(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    raw = Path(relative)
    if raw.parts and raw.parts[0].lower() == ".agent-backups":
        raise SandboxError("内部备份目录不能直接操作")
    candidate = (raw if raw.is_absolute() else root / raw).resolve(strict=must_exist)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SandboxError("路径超出已选择的工作区") from exc
    return candidate


def approval_key(tool: str, arguments: dict[str, Any]) -> str:
    target = arguments.get("path") or arguments.get("source") or arguments.get("command") or ""
    return f"{tool}:{target}"


def _result(success: bool, data: dict[str, Any] | None = None, *, error_code: str | None = None, error_message: str | None = None, retryable: bool = False, truncated: bool = False, started: float | None = None) -> dict[str, Any]:
    data = data or {}
    status = "ok" if success else "error"
    return {"success": success, "status": status, "data": data, **data, "error_code": error_code, "error_message": error_message, "error": error_message, "retryable": retryable, "truncated": truncated, "metadata": {"duration_ms": round((time.perf_counter() - started) * 1000) if started else 0}}


def _backup_root(root: Path) -> Path:
    path = root / ".agent-backups"
    path.mkdir(exist_ok=True)
    return path


def _save_backup(root: Path, operation: str, paths: list[Path]) -> str:
    change_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    folder = _backup_root(root) / change_id
    folder.mkdir()
    entries = []
    for index, path in enumerate(paths):
        existed = path.exists()
        backup = None
        if existed and path.is_file():
            backup = f"{index}.bak"
            shutil.copy2(path, folder / backup)
        entries.append({"path": str(path.relative_to(root)), "existed": existed, "backup": backup})
    (folder / "manifest.json").write_text(json.dumps({"id": change_id, "operation": operation, "entries": entries}, ensure_ascii=False), encoding="utf-8")
    return change_id


def _undo(root: Path) -> dict[str, Any]:
    folders = sorted((item for item in _backup_root(root).iterdir() if (item / "manifest.json").exists()), key=lambda item: (item / "manifest.json").stat().st_mtime_ns, reverse=True)
    if not folders:
        raise SandboxError("没有可撤销的文件操作")
    folder = folders[0]
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    restored = []
    for entry in reversed(manifest["entries"]):
        target = safe_path(root, entry["path"])
        if entry["existed"] and entry["backup"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / entry["backup"], target)
        elif target.exists() and target.is_file():
            target.unlink()
        restored.append(entry["path"])
    shutil.rmtree(folder)
    return {"change_id": manifest["id"], "restored": restored}


def _search(root: Path, base: Path, query: str, pattern: str) -> tuple[list[dict[str, Any]], bool]:
    results: list[dict[str, Any]] = []
    lowered = query.lower()
    for path in base.rglob("*"):
        relative_path = path.relative_to(root)
        if any(part in IGNORED_DIRECTORIES for part in relative_path.parts) or not path.is_file() or not fnmatch.fnmatch(path.name, pattern):
            continue
        relative = str(relative_path)
        if lowered in path.name.lower():
            results.append({"path": relative, "line": None, "text": "文件名匹配"})
        if path.stat().st_size > 1_000_000 or b"\x00" in path.read_bytes()[:4096]:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if lowered in line.lower():
                results.append({"path": relative, "line": number, "text": line[:300]})
                if len(results) >= 200:
                    return results, True
    return results, False


def execute_tool(workspace: str, mode: str, tool: str, arguments: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    root = workspace_root(workspace)
    try:
        spec = validate_arguments(tool, arguments)
    except ToolValidationError as exc:
        return _result(False, error_code="invalid_arguments", error_message=str(exc), started=started)
    mode = {"confirm": "ask", "auto": "full", "readonly": "ask"}.get(mode, mode)
    if requires_confirmation(mode, spec.risk) and not approved:
        return {"success": False, "status": "confirmation_required", "approval_key": approval_key(tool, arguments), "tool": tool, "risk": spec.risk, "arguments": arguments, "impact": arguments.get("path") or arguments.get("source") or arguments.get("command") or "当前工作区"}

    try:
        if tool == "list_files":
            path = safe_path(root, str(arguments.get("path", ".")), must_exist=True)
            if not path.is_dir(): raise SandboxError("目标不是目录")
            items = [{"name": item.name, "path": str(item.relative_to(root)), "type": "directory" if item.is_dir() else "file", "size": item.stat().st_size if item.is_file() else None} for item in sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower()))[:500] if item.name != ".agent-backups"]
            return _result(True, {"items": items}, started=started)
        if tool == "search_files":
            matches, truncated = _search(root, safe_path(root, str(arguments.get("path", ".")), must_exist=True), str(arguments["query"]).strip(), str(arguments.get("glob", "*")))
            return _result(True, {"matches": matches}, truncated=truncated, started=started)
        if tool == "read_file":
            path = safe_path(root, str(arguments["path"]), must_exist=True)
            if path.stat().st_size > 2_000_000: raise SandboxError("文件超过 2 MB，请使用搜索或缩小读取范围")
            raw = path.read_bytes()
            if b"\x00" in raw[:4096]: raise SandboxError("二进制文件不能作为文本读取")
            lines = raw.decode("utf-8", errors="replace").splitlines()
            start, end = max(int(arguments.get("start_line") or 1), 1), min(int(arguments.get("end_line") or len(lines)), len(lines))
            content = "\n".join(lines[start - 1:end]); truncated = len(content) > 40_000
            return _result(True, {"path": str(path.relative_to(root)), "start_line": start, "end_line": end, "content": content[:40_000]}, truncated=truncated, started=started)
        if tool == "file_metadata":
            path = safe_path(root, str(arguments["path"]), must_exist=True); stat = path.stat(); sample = path.read_bytes()[:4096] if path.is_file() else b""
            encoding = "binary" if b"\x00" in sample else ("utf-8" if not sample or _is_utf8(sample) else "unknown")
            return _result(True, {"path": str(path.relative_to(root)), "type": "directory" if path.is_dir() else "file", "size": stat.st_size, "modified_at": stat.st_mtime, "encoding": encoding}, started=started)
        if tool == "file_diff":
            path = safe_path(root, str(arguments["path"])); before = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
            after = str(arguments["content"]).splitlines(); diff = "\n".join(difflib.unified_diff(before, after, fromfile=f"a/{arguments['path']}", tofile=f"b/{arguments['path']}", lineterm=""))
            return _result(True, {"path": str(arguments["path"]), "diff": diff[:40_000]}, truncated=len(diff) > 40_000, started=started)
        if tool == "write_file":
            content = str(arguments["content"])
            if len(content.encode("utf-8")) > 5_000_000: raise SandboxError("单次写入不能超过 5 MB")
            path = safe_path(root, str(arguments["path"])); path.parent.mkdir(parents=True, exist_ok=True); change_id = _save_backup(root, tool, [path])
            fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle: handle.write(content); handle.flush(); os.fsync(handle.fileno())
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name): os.unlink(temp_name)
            return _result(True, {"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "change_id": change_id}, started=started)
        if tool in {"copy_file", "move_file"}:
            source = safe_path(root, str(arguments["source"]), must_exist=True); destination = safe_path(root, str(arguments["destination"])); destination.parent.mkdir(parents=True, exist_ok=True)
            if not source.is_file(): raise SandboxError("复制和移动工具仅支持单个文件")
            change_id = _save_backup(root, tool, [source, destination])
            (shutil.copy2 if tool == "copy_file" else shutil.move)(str(source), str(destination))
            return _result(True, {"source": str(arguments["source"]), "destination": str(arguments["destination"]), "change_id": change_id}, started=started)
        if tool == "create_directory":
            path = safe_path(root, str(arguments["path"])); path.mkdir(parents=True, exist_ok=True)
            return _result(True, {"path": str(path.relative_to(root))}, started=started)
        if tool == "delete_file":
            path = safe_path(root, str(arguments["path"]), must_exist=True)
            if path.is_dir(): raise SandboxError("禁止递归删除目录")
            change_id = _save_backup(root, tool, [path]); path.unlink()
            return _result(True, {"path": str(arguments["path"]), "change_id": change_id}, started=started)
        if tool == "undo_file_change":
            return _result(True, _undo(root), started=started)
        if tool == "run_command":
            cwd = safe_path(root, str(arguments.get("cwd", ".")), must_exist=True); command = str(arguments["command"]).strip()
            if Path(command).name.lower() in BLOCKED_COMMANDS: raise SandboxError("该命令被安全策略禁止")
            timeout = min(max(int(arguments.get("timeout", 60)), 1), 120)
            process = subprocess.run([command, *[str(item) for item in arguments.get("args", [])]], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, shell=False)
            stdout, stderr = process.stdout[-20_000:], process.stderr[-20_000:]
            return _result(process.returncode == 0, {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr}, error_code=None if process.returncode == 0 else "command_failed", error_message=None if process.returncode == 0 else (stderr or f"退出码 {process.returncode}"), retryable=False, truncated=len(process.stdout) > 20_000 or len(process.stderr) > 20_000, started=started)
    except subprocess.TimeoutExpired:
        return _result(False, error_code="tool_timeout", error_message="命令执行超时并已终止", retryable=True, started=started)
    except (OSError, SandboxError, KeyError, ValueError) as exc:
        return _result(False, error_code="tool_error", error_message=str(exc), started=started)
    return _result(False, error_code="unknown_tool", error_message=f"未知工具：{tool}", started=started)


def _is_utf8(data: bytes) -> bool:
    try: data.decode("utf-8"); return True
    except UnicodeDecodeError: return False
