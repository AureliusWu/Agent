from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from .database import connect, now_iso
from .tool_registry import Risk, requires_confirmation


ApprovalScope = Literal["once", "task", "session"]
APPROVAL_TTL_SECONDS = 600


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    confirmed: bool
    confirmation: dict[str, Any] | None = None


def _arguments_hash(arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_scope(scope: str, risk: Risk, conversation_id: int | None, task_id: str | None) -> ApprovalScope:
    if scope not in {"once", "task", "session"}:
        return "once"
    if risk == "critical":
        return "once"
    if scope == "task" and not task_id:
        return "once"
    if scope == "session" and conversation_id is None:
        return "once"
    return scope  # type: ignore[return-value]


def _issue(
    *,
    conversation_id: int | None,
    task_id: str | None,
    tool: str,
    arguments: dict[str, Any],
    risk: Risk,
    source: str,
    impact: str,
) -> PermissionDecision:
    token = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute(
            "INSERT INTO approval_grants(token_hash, conversation_id, task_id, tool, arguments_hash, risk, scope, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (_token_hash(token), conversation_id, task_id, tool, _arguments_hash(arguments), risk, "pending", now_iso(), time.time() + APPROVAL_TTL_SECONDS),
        )
    scopes = ["once"] if risk == "critical" else ["once", *( ["task"] if task_id else []), *( ["session"] if conversation_id is not None else [])]
    return PermissionDecision(False, False, {
        "success": False,
        "status": "confirmation_required",
        "approval_key": token,
        "tool": tool,
        "risk": risk,
        "source": source,
        "arguments": arguments,
        "impact": impact,
        "workspace_scope": "当前授权工作区",
        "allowed_scopes": scopes,
        "expires_in_seconds": APPROVAL_TTL_SECONDS,
    })


def authorize(
    *,
    mode: str,
    risk: Risk,
    tool: str,
    arguments: dict[str, Any],
    conversation_id: int | None = None,
    task_id: str | None = None,
    approval_tokens: list[str] | None = None,
    approval_scope: str = "once",
    source: str = "builtin",
    impact: str = "当前工作区",
) -> PermissionDecision:
    if not requires_confirmation(mode, risk):
        return PermissionDecision(True, False)

    arguments_hash = _arguments_hash(arguments)
    for token in approval_tokens or []:
        with connect() as db:
            row = db.execute("SELECT * FROM approval_grants WHERE token_hash=?", (_token_hash(token),)).fetchone()
            if not row or row["expires_at"] < time.time() or row["tool"] != tool or row["arguments_hash"] != arguments_hash:
                continue
            if row["conversation_id"] != conversation_id:
                continue
            scope = _valid_scope(approval_scope if row["scope"] == "pending" else row["scope"], risk, conversation_id, task_id)
            if scope in {"once", "task"} and row["task_id"] != task_id:
                continue
            if row["scope"] == "pending":
                db.execute("UPDATE approval_grants SET scope=? WHERE id=?", (scope, row["id"]))
            if scope == "once":
                if row["consumed_at"]:
                    continue
                db.execute("UPDATE approval_grants SET consumed_at=? WHERE id=?", (now_iso(), row["id"]))
            return PermissionDecision(True, True)

    return _issue(conversation_id=conversation_id, task_id=task_id, tool=tool, arguments=arguments, risk=risk, source=source, impact=impact)
