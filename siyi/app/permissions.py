from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from .database import connect, now_iso
from app.tools.registry import Risk, requires_confirmation
from app.security.trust import redact_payload


ApprovalScope = Literal["once", "task", "session"]
APPROVAL_TTL_SECONDS = 600


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    confirmed: bool
    confirmation: dict[str, Any] | None = None
    capability: dict[str, Any] | None = None


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


def _capability(
    *,
    workspace: str,
    tool: str,
    arguments: dict[str, Any],
    risk: Risk,
    source: str,
    expires_at: float,
) -> dict[str, Any]:
    paths = [
        str(arguments[key])
        for key in ("path", "source", "destination", "cwd")
        if arguments.get(key) not in (None, "")
    ]
    commands = []
    if tool == "run_command":
        command_scope, _ = redact_payload({
            "command": str(arguments.get("command") or ""),
            "args": [str(item) for item in arguments.get("args") or []],
        })
        commands.append(command_scope)
    return {
        "version": 1,
        "workspace": workspace,
        "tools": [tool],
        "allowed_paths": paths,
        "allowed_commands": commands,
        "network": {"allowed": source in {"mcp", "plugin:voice"}, "source": source},
        "risk": risk,
        "expires_at": expires_at,
    }


def _issue(
    *,
    conversation_id: int | None,
    task_id: str | None,
    tool: str,
    arguments: dict[str, Any],
    risk: Risk,
    source: str,
    impact: str,
    workspace: str,
) -> PermissionDecision:
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + APPROVAL_TTL_SECONDS
    capability = _capability(
        workspace=workspace,
        tool=tool,
        arguments=arguments,
        risk=risk,
        source=source,
        expires_at=expires_at,
    )
    with connect() as db:
        db.execute(
            "INSERT INTO approval_grants(token_hash, conversation_id, task_id, tool, arguments_hash, workspace, capabilities, risk, scope, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                _token_hash(token),
                conversation_id,
                task_id,
                tool,
                _arguments_hash(arguments),
                workspace,
                json.dumps(capability, ensure_ascii=False, sort_keys=True),
                risk,
                "pending",
                now_iso(),
                expires_at,
            ),
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
        "workspace_scope": workspace or "当前授权工作区",
        "capability": capability,
        "allowed_scopes": scopes,
        "expires_in_seconds": APPROVAL_TTL_SECONDS,
    }, capability)


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
    workspace: str = "",
) -> PermissionDecision:
    if mode == "readonly" and (risk != "low" or source != "builtin"):
        return PermissionDecision(
            False,
            False,
            {
                "success": False,
                "status": "blocked",
                "error_code": "read_only_mode",
                "error_message": "当前工作区为只读模式，写入和外部副作用工具已禁用",
                "tool": tool,
                "risk": risk,
                "source": source,
            },
        )
    if not requires_confirmation(mode, risk):
        capability = _capability(
            workspace=workspace,
            tool=tool,
            arguments=arguments,
            risk=risk,
            source=source,
            expires_at=time.time() + APPROVAL_TTL_SECONDS,
        )
        return PermissionDecision(True, False, capability=capability)

    arguments_hash = _arguments_hash(arguments)
    for token in approval_tokens or []:
        with connect() as db:
            row = db.execute("SELECT * FROM approval_grants WHERE token_hash=?", (_token_hash(token),)).fetchone()
            if not row or row["expires_at"] < time.time() or row["tool"] != tool or row["arguments_hash"] != arguments_hash:
                continue
            if row["conversation_id"] != conversation_id:
                continue
            if str(row["workspace"] or "") != workspace:
                continue
            scope = _valid_scope(approval_scope if row["scope"] == "pending" else row["scope"], risk, conversation_id, task_id)
            if scope in {"once", "task"} and row["task_id"] != task_id:
                continue
            if row["scope"] == "pending":
                db.execute(
                    "UPDATE approval_grants SET scope=?, task_id=? WHERE id=?",
                    (scope, None if scope == "session" else task_id, row["id"]),
                )
            if scope == "once":
                if row["consumed_at"]:
                    continue
                db.execute("UPDATE approval_grants SET consumed_at=? WHERE id=?", (now_iso(), row["id"]))
            capability = json.loads(row["capabilities"] or "{}")
            if capability.get("workspace") != workspace or tool not in capability.get("tools", []):
                continue
            return PermissionDecision(True, True, capability=capability)

    return _issue(
        conversation_id=conversation_id,
        task_id=task_id,
        tool=tool,
        arguments=arguments,
        risk=risk,
        source=source,
        impact=impact,
        workspace=workspace,
    )


def expire_task_capabilities(task_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM approval_grants WHERE task_id=?", (task_id,))
