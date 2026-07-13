from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .database import connect, now_iso, rows
from .sandbox import safe_path, workspace_root


IGNORED_PARTS = {".git", ".agent-backups", ".venv", "venv", "node_modules", "target", "build", "dist", "__pycache__"}
DEPENDENCY_FILES = {
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
}
MUTATION_TOOLS = {
    "create_file",
    "write_file",
    "replace_text",
    "apply_patch",
    "copy_file",
    "move_file",
    "rename_file",
    "create_directory",
    "delete_file",
    "undo_file_change",
    "undo_task_changes",
    "remember_workspace",
    "forget_workspace_memory",
}
SIDE_EFFECT_TOOLS = {*MUTATION_TOOLS, "run_command"}
CHECKPOINT_STATE_DEFAULTS: dict[str, Any] = {
    "goal": "",
    "current_phase": "analysis",
    "completed_steps": [],
    "pending_steps": [],
    "modified_files": [],
    "created_files": [],
    "deleted_files": [],
    "commands_run": [],
    "known_errors": [],
    "test_status": {},
    "build_status": {},
    "verification_status": {},
    "context_summary": "",
    "workspace_hash": "",
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_record(root: Path, relative: str) -> dict[str, Any]:
    try:
        path = safe_path(root, relative)
    except Exception as exc:
        return {"path": relative, "accessible": False, "error": str(exc)}
    if not path.exists():
        return {"path": relative, "accessible": True, "exists": False}
    if path.is_dir():
        return {"path": relative, "accessible": True, "exists": True, "type": "directory"}
    raw = path.read_bytes()
    return {
        "path": relative,
        "accessible": True,
        "exists": True,
        "type": "file",
        "size": len(raw),
        "sha256": _sha256(raw),
    }


def _run_git(root: Path, args: list[str]) -> str | None:
    if not (root / ".git").exists():
        return None
    try:
        process = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return process.stdout if process.returncode == 0 else None


def _non_git_inventory(root: Path, limit: int = 2_000) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative.parts) or not path.is_file():
            continue
        stat = path.stat()
        inventory.append({"path": relative.as_posix(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        if len(inventory) >= limit:
            break
    return inventory


def workspace_evidence(workspace: str, tracked_paths: Iterable[str] = ()) -> dict[str, Any]:
    root = workspace_root(workspace)
    normalized_paths = sorted({str(path).replace("\\", "/").lstrip("./") for path in tracked_paths if path})
    dependency_paths = sorted(path.name for path in root.iterdir() if path.is_file() and path.name in DEPENDENCY_FILES)
    key_files = [_file_record(root, path) for path in sorted(set(normalized_paths) | set(dependency_paths))]
    status = _run_git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status is not None:
        status_lines = [line for line in status.splitlines() if ".agent-backups/" not in line.replace("\\", "/")]
        git_status = "\n".join(status_lines)
        head = (_run_git(root, ["rev-parse", "HEAD"]) or "").strip()
        working_diff = _run_git(root, ["diff", "--no-ext-diff", "--binary", "--"])
        staged_diff = _run_git(root, ["diff", "--cached", "--no-ext-diff", "--binary", "--"])
        untracked = []
        for line in status_lines:
            if not line.startswith("?? "):
                continue
            relative = line[3:].strip('"').replace("\\", "/")
            untracked.append(_file_record(root, relative))
        basis = {
            "kind": "git",
            "head": head,
            "status": git_status,
            "working_diff_sha256": _sha256((working_diff or "").encode("utf-8")),
            "staged_diff_sha256": _sha256((staged_diff or "").encode("utf-8")),
            "untracked": untracked,
            "key_files": key_files,
        }
    else:
        git_status = ""
        inventory = _non_git_inventory(root)
        basis = {"kind": "filesystem", "inventory": inventory, "key_files": key_files, "truncated": len(inventory) >= 2_000}
    encoded = json.dumps(basis, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return {"workspace_hash": _sha256(encoded), "git_status": git_status, "snapshot": basis}


def create_checkpoint(
    task_id: str,
    workspace: str,
    phase: str,
    reason: str,
    state: dict[str, Any],
    *,
    workspace_evidence_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = {**CHECKPOINT_STATE_DEFAULTS, **state, "current_phase": phase}
    tracked_paths = [
        *contract.get("modified_files", []),
        *contract.get("created_files", []),
        *contract.get("deleted_files", []),
        *contract.get("expected_paths", []),
    ]
    evidence = workspace_evidence_override or workspace_evidence(workspace, tracked_paths)
    contract["workspace_hash"] = evidence["workspace_hash"]
    contract["workspace_snapshot"] = evidence["snapshot"]
    encoded = json.dumps(contract, ensure_ascii=False, default=str)
    stamp = now_iso()
    with connect() as db:
        task = db.execute("SELECT id FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("任务不存在，无法创建检查点")
        sequence = int(db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_checkpoints WHERE task_id=?", (task_id,)).fetchone()[0])
        cursor = db.execute(
            "INSERT INTO task_checkpoints(task_id, sequence, phase, reason, state, workspace_hash, git_status, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, sequence, phase, reason, encoded, evidence["workspace_hash"], evidence["git_status"], stamp),
        )
        db.execute(
            "UPDATE agent_tasks SET current_phase=?, checkpoint_sequence=?, updated_at=? WHERE id=?",
            (phase, sequence, stamp, task_id),
        )
    return {
        "id": int(cursor.lastrowid),
        "task_id": task_id,
        "sequence": sequence,
        "phase": phase,
        "reason": reason,
        "state": contract,
        "workspace_hash": evidence["workspace_hash"],
        "git_status": evidence["git_status"],
        "created_at": stamp,
    }


def load_checkpoint(task_id: str, sequence: int | None = None) -> dict[str, Any] | None:
    if sequence is None:
        records = rows("SELECT * FROM task_checkpoints WHERE task_id=? ORDER BY sequence DESC LIMIT 1", (task_id,))
    else:
        records = rows("SELECT * FROM task_checkpoints WHERE task_id=? AND sequence=?", (task_id, sequence))
    if not records:
        return None
    record = records[0]
    record["state"] = json.loads(record["state"])
    return record


def list_checkpoints(task_id: str) -> list[dict[str, Any]]:
    records = rows(
        "SELECT id, task_id, sequence, phase, reason, workspace_hash, git_status, created_at FROM task_checkpoints WHERE task_id=? ORDER BY sequence DESC",
        (task_id,),
    )
    return records


def validate_resume(checkpoint: dict[str, Any], workspace: str) -> dict[str, Any]:
    state = checkpoint.get("state") or {}
    tracked_paths = [
        *state.get("modified_files", []),
        *state.get("created_files", []),
        *state.get("deleted_files", []),
        *state.get("expected_paths", []),
    ]
    current = workspace_evidence(workspace, tracked_paths)
    expected_hash = checkpoint.get("workspace_hash") or state.get("workspace_hash")
    matches = bool(expected_hash and current["workspace_hash"] == expected_hash)
    return {
        "matches": matches,
        "expected_workspace_hash": expected_hash,
        "current_workspace_hash": current["workspace_hash"],
        "checkpoint_git_status": checkpoint.get("git_status") or "",
        "current_git_status": current["git_status"],
        "dependencies_changed": (state.get("workspace_snapshot") or {}).get("key_files") != current["snapshot"].get("key_files"),
    }


def operation_execution_id(task_id: str, call: dict[str, Any]) -> str:
    function = call.get("function") or {}
    raw = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        arguments = {"_invalid_json": raw}
    basis = {
        "task_id": task_id,
        "tool_call_id": call.get("id"),
        "tool": function.get("name"),
        "arguments": arguments,
    }
    return _sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))


def prepare_operation(
    task_id: str,
    checkpoint_sequence: int,
    call: dict[str, Any],
    arguments: dict[str, Any],
    *,
    side_effect: bool,
) -> dict[str, Any]:
    execution_id = operation_execution_id(task_id, call)
    arguments_hash = _sha256(json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    stamp = now_iso()
    if not side_effect:
        return {
            "execution_id": execution_id,
            "task_id": task_id,
            "checkpoint_sequence": checkpoint_sequence,
            "tool_call_id": str(call.get("id") or ""),
            "tool": str((call.get("function") or {}).get("name") or ""),
            "arguments_hash": arguments_hash,
            "status": "ephemeral",
            "side_effect": 0,
            "started_at": stamp,
            "created": True,
            "result": None,
        }
    with connect() as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO task_operations(execution_id, task_id, checkpoint_sequence, tool_call_id, tool, arguments_hash, status, side_effect, started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (execution_id, task_id, checkpoint_sequence, str(call.get("id") or ""), str((call.get("function") or {}).get("name") or ""), arguments_hash, "running", int(side_effect), stamp),
        )
        record = dict(db.execute("SELECT * FROM task_operations WHERE execution_id=?", (execution_id,)).fetchone())
    record["created"] = cursor.rowcount == 1
    record["result"] = json.loads(record["result"]) if record.get("result") else None
    return record


def set_operation_status(execution_id: str, status: str, result: dict[str, Any] | None = None) -> None:
    finished = now_iso() if status in {"completed", "failed", "cancelled", "uncertain", "waiting_confirmation"} else None
    with connect() as db:
        db.execute(
            "UPDATE task_operations SET status=?, result=?, finished_at=? WHERE execution_id=?",
            (status, json.dumps(result, ensure_ascii=False, default=str) if result is not None else None, finished, execution_id),
        )


def restart_operation(execution_id: str) -> None:
    with connect() as db:
        db.execute(
            "UPDATE task_operations SET status='running', result=NULL, started_at=?, finished_at=NULL WHERE execution_id=?",
            (now_iso(), execution_id),
        )


def operation_status(task_id: str, execution_id: str) -> dict[str, Any] | None:
    records = rows("SELECT * FROM task_operations WHERE task_id=? AND execution_id=?", (task_id, execution_id))
    if not records:
        return None
    record = records[0]
    record["result"] = json.loads(record["result"]) if record.get("result") else None
    return record


def running_operation(task_id: str) -> dict[str, Any] | None:
    records = rows("SELECT * FROM task_operations WHERE task_id=? AND status='running' ORDER BY started_at DESC LIMIT 1", (task_id,))
    return records[0] if records else None
