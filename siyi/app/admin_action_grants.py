from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .database import connect, now_iso


MEMORY_OPERATIONS = {
    "memory.create",
    "memory.update",
    "memory.delete",
    "memory.search_sensitive",
    "memory_candidate.accept",
}

# Management actions are deliberately kept separate from ordinary Tool grants.
# They mutate the Agent's trusted configuration or may execute an MCP probe, so
# every call must use a short-lived, payload-bound, single-use administrator
# grant.  Keeping this registry next to the grant implementation also prevents a
# route from silently inventing an unreviewed management operation.
MANAGEMENT_OPERATIONS = {
    "mcp.register",
    "mcp.enable",
    "mcp.disable",
    "mcp.delete",
    "mcp.test",
    "extension.install",
    "extension.enable",
    "extension.disable",
    "extension.uninstall",
    "extension.rollback",
    "skill.install",
    "skill.enable",
    "skill.disable",
    "skill.uninstall",
}

ALLOWED_OPERATIONS = MEMORY_OPERATIONS | MANAGEMENT_OPERATIONS


class AdminActionGrantError(PermissionError):
    pass


@dataclass(frozen=True)
class AdminActionAuthorization:
    grant_id: int
    operation: str
    target_id: str
    payload_hash: str
    ui_session_id: str


def _canonical_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_payload(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(_canonical_payload(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def issue_admin_action_grant(
    *, operation: str, target_id: str, payload: dict[str, Any], ui_session_id: str, ttl_seconds: int = 90
) -> dict[str, Any]:
    if operation not in ALLOWED_OPERATIONS:
        raise AdminActionGrantError("Unsupported administrator operation")
    if not target_id.strip() or not ui_session_id.strip():
        raise AdminActionGrantError("Administrator target and UI session are required")
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = time.time() + max(15, min(ttl_seconds, 300))
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO admin_action_grants(token_hash,operation,target_id,payload_hash,ui_session_id,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (token_hash, operation, target_id, payload_hash(payload), ui_session_id, now_iso(), expires_at),
        )
    return {"grant_token": token, "grant_id": int(cursor.lastrowid), "expires_at": expires_at}


def consume_admin_action_grant(
    token: str,
    *,
    operation: str,
    target_id: str,
    payload: dict[str, Any],
    ui_session_id: str,
) -> AdminActionAuthorization:
    if not token:
        raise AdminActionGrantError("Administrator action grant is required")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expected_payload_hash = payload_hash(payload)
    consumed_at = now_iso()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM admin_action_grants WHERE token_hash=? AND consumed_at IS NULL AND expires_at>=?",
            (token_hash, time.time()),
        ).fetchone()
        if row is None:
            raise AdminActionGrantError("Administrator action grant is invalid, expired, or already consumed")
        record = dict(row)
        if (
            record["operation"] != operation
            or record["target_id"] != target_id
            or record["payload_hash"] != expected_payload_hash
            or record["ui_session_id"] != ui_session_id
        ):
            raise AdminActionGrantError("Administrator action grant does not match this operation")
        updated = db.execute(
            "UPDATE admin_action_grants SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
            (consumed_at, record["id"]),
        )
        if updated.rowcount != 1:
            raise AdminActionGrantError("Administrator action grant was already consumed")
    return AdminActionAuthorization(
        grant_id=int(record["id"]),
        operation=operation,
        target_id=target_id,
        payload_hash=expected_payload_hash,
        ui_session_id=ui_session_id,
    )


def require_admin_authorization(
    authorization: AdminActionAuthorization | None,
    *,
    operation: str,
    target_id: str,
    payload: dict[str, Any],
) -> None:
    if (
        authorization is None
        or authorization.operation != operation
        or authorization.target_id != target_id
        or authorization.payload_hash != payload_hash(payload)
    ):
        raise AdminActionGrantError("A matching consumed administrator action grant is required")
