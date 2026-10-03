from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


MUTATION_TOOLS = {
    "create_file",
    "write_file",
    "synthesize_speech",
    "replace_text",
    "apply_patch",
    "copy_file",
    "move_file",
    "rename_file",
    "create_directory",
    "delete_file",
    "undo_file_change",
    "undo_task_changes",
    "restore_security_snapshot",
    "run_command",
    "create_worktree",
    "remove_worktree",
}
READ_TOOLS = {"discover_tools", "read_file", "read_file_range", "file_metadata", "file_info", "list_files", "list_directory"}


@dataclass(frozen=True)
class ToolReceipt:
    receipt_version: int
    tool: str
    operation_kind: str
    success: bool
    status: str
    changed_files: tuple[str, ...]
    observed_files: tuple[str, ...]
    version_before: Any
    version_after: Any
    change_id: str | None
    exit_code: int | None
    artifact_id: str | None
    total_bytes: int | None
    truncated: bool
    error_code: str | None
    error_fingerprint: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _paths(data: dict[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for key in ("path", "source", "destination"):
        value = data.get(key)
        if isinstance(value, str) and value not in paths:
            paths.append(value)
    return tuple(paths)


def _error_fingerprint(tool: str, result: dict[str, Any], error_code: str | None) -> str | None:
    if result.get("success"):
        return None
    stable = {
        "tool": tool,
        "error_code": error_code,
        "status": result.get("status"),
        "retryable": bool(result.get("retryable")),
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_tool_receipt(tool: str, result: dict[str, Any]) -> ToolReceipt:
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    operation_kind = "mutation" if tool in MUTATION_TOOLS else ("read" if tool in READ_TOOLS else "external")
    paths = _paths(data if isinstance(data, dict) else {})
    error_code = str(result["error_code"]) if result.get("error_code") else None
    return ToolReceipt(
        receipt_version=2,
        tool=tool,
        operation_kind=operation_kind,
        success=bool(result.get("success")),
        status=str(result.get("status") or ("ok" if result.get("success") else "error")),
        changed_files=paths if operation_kind == "mutation" else (),
        observed_files=paths if operation_kind == "read" else (),
        version_before=data.get("version_before") if isinstance(data, dict) else None,
        version_after=data.get("version_after") if isinstance(data, dict) else None,
        change_id=str(data["change_id"]) if isinstance(data, dict) and data.get("change_id") else None,
        exit_code=int(data["exit_code"]) if isinstance(data, dict) and data.get("exit_code") is not None else None,
        artifact_id=str(data["artifact_id"]) if isinstance(data, dict) and data.get("artifact_id") else None,
        total_bytes=int(data["total_bytes"]) if isinstance(data, dict) and data.get("total_bytes") is not None else None,
        truncated=bool(result.get("truncated") or result.get("_truncated")),
        error_code=error_code,
        error_fingerprint=_error_fingerprint(tool, result, error_code),
    )
