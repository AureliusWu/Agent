from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from app.database import connect, now_iso, rows
from app.security.trust import redact_payload


Role = Literal["planner", "executor", "reviewer", "verifier", "recovery_coordinator"]
MessageType = Literal["plan", "work_result", "review", "verification", "recovery", "blocked"]

ROLE_CAPABILITIES: dict[Role, tuple[str, ...]] = {
    "planner": ("read", "plan"),
    "executor": ("read", "execute", "write"),
    "reviewer": ("read", "compare_evidence"),
    "verifier": ("read", "verify"),
    "recovery_coordinator": ("read", "retry", "repair", "rollback", "block"),
}
ALLOWED_MESSAGES: dict[Role, tuple[Role, ...]] = {
    "planner": ("executor", "reviewer"),
    "executor": ("reviewer", "verifier", "recovery_coordinator"),
    "reviewer": ("executor", "verifier", "recovery_coordinator"),
    "verifier": ("executor", "recovery_coordinator"),
    "recovery_coordinator": ("executor", "reviewer", "verifier"),
}


@dataclass(frozen=True)
class RoleMessage:
    id: str
    task_id: str
    sender_role: Role
    recipient_role: Role
    message_type: MessageType
    payload: dict[str, Any]
    correlation_id: str
    created_at: str


def register_role(task_id: str, role: Role, status: str = "pending", attempt: int = 0) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO task_roles(task_id,role,status,capabilities,attempt,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(task_id,role) DO UPDATE SET status=excluded.status,attempt=excluded.attempt,updated_at=excluded.updated_at",
            (task_id, role, status, json.dumps(ROLE_CAPABILITIES[role]), attempt, now_iso()),
        )


def send_role_message(
    task_id: str,
    sender_role: Role,
    recipient_role: Role,
    message_type: MessageType,
    payload: dict[str, Any],
    *,
    correlation_id: str | None = None,
) -> RoleMessage:
    if recipient_role not in ALLOWED_MESSAGES[sender_role]:
        raise ValueError(f"role transition is not allowed: {sender_role}->{recipient_role}")
    cleaned, _ = redact_payload(payload)
    if not isinstance(cleaned, dict):
        raise ValueError("role message payload must be an object")
    message = RoleMessage(
        uuid.uuid4().hex,
        task_id,
        sender_role,
        recipient_role,
        message_type,
        cleaned,
        correlation_id or uuid.uuid4().hex,
        now_iso(),
    )
    with connect() as db:
        db.execute(
            "INSERT INTO role_messages(id,task_id,sender_role,recipient_role,message_type,payload,correlation_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (message.id, task_id, sender_role, recipient_role, message_type, json.dumps(cleaned, ensure_ascii=False), message.correlation_id, message.created_at),
        )
    return message


def review_execution_scope(task_id: str, expected_paths: tuple[str, ...]) -> dict[str, Any]:
    receipts = rows("SELECT receipt_json,status FROM tool_receipts WHERE task_id=? ORDER BY created_at", (task_id,))
    actual_paths: set[str] = set()
    failed_receipts = 0
    for receipt in receipts:
        if receipt["status"] != "completed":
            failed_receipts += 1
        try:
            payload = json.loads(receipt["receipt_json"])
        except (TypeError, ValueError):
            continue
        for path in payload.get("affected_paths") or payload.get("paths") or ():
            actual_paths.add(str(path).replace("\\", "/"))
    expected = {path.replace("\\", "/") for path in expected_paths}
    out_of_scope = sorted(actual_paths - expected) if expected else sorted(actual_paths)
    return {
        "status": "rejected" if out_of_scope or failed_receipts else "accepted",
        "expected_paths": sorted(expected),
        "actual_paths": sorted(actual_paths),
        "out_of_scope": out_of_scope,
        "failed_receipts": failed_receipts,
        "evidence_count": len(receipts),
    }


def recovery_decision(*, attempt: int, max_attempts: int, reversible: bool, evidence_changed: bool) -> str:
    if attempt >= max_attempts:
        return "rollback" if reversible else "block"
    if evidence_changed:
        return "repair"
    return "retry"


def role_trace(task_id: str) -> dict[str, Any]:
    roles = rows("SELECT role,status,capabilities,attempt,updated_at FROM task_roles WHERE task_id=? ORDER BY role", (task_id,))
    messages = rows("SELECT id,sender_role,recipient_role,message_type,payload,correlation_id,created_at FROM role_messages WHERE task_id=? ORDER BY created_at", (task_id,))
    for item in (*roles, *messages):
        key = "capabilities" if "capabilities" in item else "payload"
        item[key] = json.loads(item[key])
    return {"roles": roles, "messages": messages}
