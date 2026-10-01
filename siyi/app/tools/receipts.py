from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from dataclasses import field
from typing import Any

from app.database import now_iso, sanitize_details


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
    "undo_file_batch",
    "restore_security_snapshot",
    "run_command",
    "create_worktree",
    "remove_worktree",
    "file_batch",
    "artifact.markdown.create",
    "artifact.docx.create",
    "artifact.docx.edit",
    "artifact.pdf.create",
    "artifact.pdf.merge",
    "artifact.pptx.create",
    "artifact.pptx.edit",
    "artifact.render",
}
READ_TOOLS = {
    "read_file", "read_file_range", "file_metadata", "file_info",
    "list_files", "list_directory", "artifact.pdf.extract", "artifact.validate",
}


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
    receipt_id: str = ""
    task_id: str | None = None
    tool_call_id: str | None = None
    standard_status: str = "FAILED"
    normalized_arguments: dict[str, Any] = field(default_factory=dict)
    permission_decision: str = "unknown"
    risk_level: str = "unknown"
    rollback: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=lambda: {"status": "pending"})
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _paths(data: dict[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for key in ("path", "source", "destination"):
        value = data.get(key)
        if isinstance(value, str) and value not in paths:
            paths.append(value)
    values = data.get("paths")
    if isinstance(values, list):
        paths.extend(
            str(value)
            for value in values
            if isinstance(value, str) and value not in paths
        )
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


def _standard_status(result: dict[str, Any]) -> str:
    if result.get("cancelled") or result.get("error_code") == "cancelled":
        return "CANCELLED"
    if result.get("timed_out") or result.get("error_code") == "tool_timeout":
        return "TIMED_OUT"
    if result.get("error_code") in {"confirmation_required", "security_block", "permission_denied"}:
        return "BLOCKED"
    if result.get("partial"):
        return "PARTIAL"
    return "SUCCESS" if result.get("success") else "FAILED"


def build_tool_receipt(
    tool: str,
    result: dict[str, Any],
    *,
    task_id: str | None = None,
    tool_call_id: str | None = None,
    arguments: dict[str, Any] | None = None,
    permission_decision: str = "unknown",
    risk_level: str = "unknown",
    started_at: str | None = None,
) -> ToolReceipt:
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    operation_kind = "mutation" if tool in MUTATION_TOOLS else ("read" if tool in READ_TOOLS else "external")
    paths = _paths(data if isinstance(data, dict) else {})
    error_code = str(result["error_code"]) if result.get("error_code") else None
    finished_at = now_iso()
    safe_arguments = sanitize_details(arguments or {})
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
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
        receipt_id=uuid.uuid4().hex,
        task_id=task_id,
        tool_call_id=tool_call_id,
        standard_status=_standard_status(result),
        normalized_arguments=safe_arguments,
        permission_decision=permission_decision,
        risk_level=risk_level,
        rollback={
            "supported": bool(isinstance(data, dict) and data.get("change_id")),
            "change_id": str(data["change_id"]) if isinstance(data, dict) and data.get("change_id") else None,
            "recoverable": bool(isinstance(data, dict) and data.get("recoverable")),
            "trash_id": str(data["trash_id"]) if isinstance(data, dict) and data.get("trash_id") else None,
        },
        verification={"status": "pending" if tool in MUTATION_TOOLS else "not_required"},
        started_at=started_at or finished_at,
        finished_at=finished_at,
        duration_ms=int(metadata.get("duration_ms") or 0),
    )
