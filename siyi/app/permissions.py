from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from .admin_action_grants import (
    AdminActionAuthorization,
    AdminActionGrantError,
    MANAGEMENT_OPERATIONS,
    consume_admin_action_grant,
    issue_admin_action_grant,
)
from .database import connect, now_iso
from app.tools.registry import Risk, requires_confirmation
from app.security.trust import redact_payload


ApprovalScope = Literal["once", "task", "session", "workspace", "always", "deny"]
PermissionName = Literal[
    "filesystem.read",
    "filesystem.write",
    "filesystem.delete",
    "process.execute",
    "network.request",
    "secret.read",
    "clipboard.read",
    "clipboard.write",
    "camera.read",
    "microphone.read",
    "connector.access",
    "skill.install",
    "skill.modify",
]
APPROVAL_TTL_SECONDS = 600
PERMISSION_NAMES = {
    "filesystem.read",
    "filesystem.write",
    "filesystem.delete",
    "process.execute",
    "network.request",
    "secret.read",
    "clipboard.read",
    "clipboard.write",
    "camera.read",
    "microphone.read",
    "connector.access",
    "skill.install",
    "skill.modify",
}
_READ_TOOLS = {
    "list_files", "list_directory", "search_files", "search_text", "read_file",
    "read_file_range", "file_metadata", "file_info", "file_diff", "view_diff",
    "compare_files", "get_repo_map", "find_symbol", "find_definition",
    "find_references", "list_module_dependencies", "find_related_tests",
    "get_call_chain", "inspect_diagnostics", "lsp_query", "list_worktrees",
    "list_file_changes", "list_security_snapshots", "preview_security_snapshot",
    "list_workspace_memories",
    "vision.local",
    "vision.describe", "vision.extract_text", "vision.analyze_chart",
    "vision.compare", "vision.inspect_ui", "vision.classify",
    "artifact.pdf.extract", "artifact.validate",
}
_DELETE_TOOLS = {
    "delete_file", "delete_directory", "remove_worktree", "forget_workspace_memory",
}


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    confirmed: bool
    confirmation: dict[str, Any] | None = None
    capability: dict[str, Any] | None = None


class AdminActionPermissionError(PermissionError):
    """Fail-closed error returned by the administrator action boundary."""

    def __init__(self, code: str, message: str, *, status_code: int = 403) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


def _readonly_hard_denied(mode: str, risk: Risk, source: str) -> bool:
    """Readonly is a hard boundary, never an approval/confirmation state."""

    return mode == "readonly" and (risk != "low" or source != "builtin")


def _same_workspace(left: str, right: str) -> bool:
    if not left or not right:
        return left == right
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass(frozen=True)
class AdminActionPermission:
    """Authoritative permission context for trusted configuration mutations.

    Management actions are intentionally not disguised as file Tools.  They use
    a short-lived, payload-bound and single-use administrator grant, while the
    persisted conversation remains the source of truth for permission mode and
    workspace scope.
    """

    conversation_id: int
    permission_mode: str
    workspace: str

    @classmethod
    def for_conversation(cls, conversation_id: int) -> "AdminActionPermission":
        with connect() as db:
            row = db.execute(
                "SELECT id,permission_mode,workspace FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise AdminActionPermissionError(
                "conversation_not_found",
                "管理员操作必须绑定现有对话",
                status_code=404,
            )
        mode = str(row["permission_mode"] or "")
        if mode not in {"readonly", "ask", "agent", "full"}:
            raise AdminActionPermissionError("invalid_permission_mode", "对话权限模式无效")
        return cls(int(row["id"]), mode, str(row["workspace"] or ""))

    def _assert_allowed(self, *, operation: str, workspace: str | None) -> None:
        if operation not in MANAGEMENT_OPERATIONS:
            raise AdminActionPermissionError(
                "unsupported_admin_action",
                "不支持的管理员操作",
                status_code=400,
            )
        if _readonly_hard_denied(self.permission_mode, "critical", "admin"):
            raise AdminActionPermissionError(
                "read_only_mode",
                "当前对话为只读模式，管理员副作用操作已禁用",
            )
        if workspace is not None and not _same_workspace(self.workspace, workspace):
            raise AdminActionPermissionError(
                "workspace_scope_mismatch",
                "管理员操作工作区与授权对话不一致",
            )

    def _grant_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "workspace": self.workspace,
            "payload": payload,
        }

    def issue(
        self,
        *,
        operation: str,
        target_id: str,
        payload: dict[str, Any],
        ui_session_id: str,
        administrator_confirmed: bool,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        self._assert_allowed(operation=operation, workspace=workspace)
        if not administrator_confirmed:
            raise AdminActionPermissionError(
                "administrator_confirmation_required",
                "需要管理员明确确认",
            )
        try:
            result = issue_admin_action_grant(
                operation=operation,
                target_id=target_id,
                payload=self._grant_payload(payload),
                ui_session_id=ui_session_id,
            )
        except AdminActionGrantError as exc:
            raise AdminActionPermissionError("invalid_admin_action", str(exc), status_code=400) from exc
        return {
            **result,
            "operation": operation,
            "target_id": target_id,
            "conversation_id": self.conversation_id,
        }

    def authorize(
        self,
        token: str,
        *,
        operation: str,
        target_id: str,
        payload: dict[str, Any],
        ui_session_id: str,
        workspace: str | None = None,
    ) -> AdminActionAuthorization:
        self._assert_allowed(operation=operation, workspace=workspace)
        try:
            return consume_admin_action_grant(
                token,
                operation=operation,
                target_id=target_id,
                payload=self._grant_payload(payload),
                ui_session_id=ui_session_id,
            )
        except AdminActionGrantError as exc:
            raise AdminActionPermissionError("admin_action_grant_denied", str(exc)) from exc


def _arguments_hash(arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def permission_for_tool(tool: str, source: str = "builtin") -> str:
    if source == "mcp":
        return "connector.access"
    if tool in _READ_TOOLS:
        return "filesystem.read"
    if tool in _DELETE_TOOLS:
        return "filesystem.delete"
    if tool == "run_command":
        return "process.execute"
    if tool in {"web_search", "web_fetch", "vision.remote"}:
        return "network.request"
    if tool in {"secret.read", "get_secret"}:
        return "secret.read"
    if tool in {"clipboard.read", "clipboard.write", "camera.read", "microphone.read"}:
        return tool
    if tool in {"skill.install", "install_skill"}:
        return "skill.install"
    if tool in {"skill.modify", "uninstall_skill", "enable_skill"}:
        return "skill.modify"
    return "filesystem.write"


def list_permission_policies(*, include_revoked: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM permission_policies"
    if not include_revoked:
        query += " WHERE revoked_at IS NULL"
    query += " ORDER BY id DESC"
    with connect() as db:
        return [dict(row) for row in db.execute(query).fetchall()]


def set_permission_policy(
    *,
    permission: str,
    effect: str,
    scope: str,
    workspace: str = "",
    tool: str = "*",
    source: str = "*",
    principal: str = "*",
) -> dict[str, Any]:
    if permission not in PERMISSION_NAMES:
        raise ValueError(f"未知权限：{permission}")
    if effect not in {"allow", "deny"}:
        raise ValueError("权限策略 effect 必须为 allow/deny")
    if scope not in {"workspace", "always"}:
        raise ValueError("持久权限范围必须为 workspace/always")
    if scope == "workspace" and not workspace:
        raise ValueError("工作区授权必须绑定工作区")
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO permission_policies(permission,effect,scope,workspace,tool,source,principal,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                permission, effect, scope, workspace if scope == "workspace" else "",
                tool or "*", source or "*", principal or "*", stamp, stamp,
            ),
        )
        row = db.execute("SELECT * FROM permission_policies WHERE id=?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def revoke_permission_policy(policy_id: int) -> bool:
    with connect() as db:
        cursor = db.execute(
            "UPDATE permission_policies SET revoked_at=?,updated_at=? WHERE id=? AND revoked_at IS NULL",
            (now_iso(), now_iso(), policy_id),
        )
    return cursor.rowcount > 0


def _matching_policy(
    *,
    permission: str,
    workspace: str,
    tool: str,
    source: str,
    principal: str,
) -> dict[str, Any] | None:
    with connect() as db:
        policies = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM permission_policies WHERE permission=? AND revoked_at IS NULL "
                "AND (scope='always' OR (scope='workspace' AND workspace=?)) "
                "AND (tool='*' OR tool=?) AND (source='*' OR source=?) "
                "AND (principal='*' OR principal=?) ORDER BY CASE effect WHEN 'deny' THEN 0 ELSE 1 END,id DESC",
                (permission, workspace, tool, source, principal),
            ).fetchall()
        ]
    return policies[0] if policies else None


def _valid_scope(
    scope: str,
    risk: Risk,
    conversation_id: int | None,
    task_id: str | None,
    workspace: str,
) -> ApprovalScope:
    if scope not in {"once", "task", "session", "workspace", "always", "deny"}:
        return "once"
    if risk == "critical" and scope != "deny":
        return "once"
    if scope == "workspace":
        return "workspace" if workspace else "once"
    if scope in {"always", "deny"}:
        return scope  # type: ignore[return-value]
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
        for key in ("path", "source", "destination", "cwd", "output_directory")
        if arguments.get(key) not in (None, "")
    ]
    for key in ("image_paths", "inputs", "paths"):
        values = arguments.get(key)
        if isinstance(values, list):
            paths.extend(str(value) for value in values)
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
        "network": {"allowed": source == "mcp", "source": source},
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
    permission = permission_for_tool(tool, source)
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
    scopes = (
        ["once"]
        if risk == "critical"
        else [
            "once",
            *(["task"] if task_id else []),
            *(["session"] if conversation_id is not None else []),
            *(["workspace"] if workspace else []),
            "always",
            "deny",
        ]
    )
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
        "permission": permission,
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
    principal: str = "*",
) -> PermissionDecision:
    if _readonly_hard_denied(mode, risk, source):
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
    permission = permission_for_tool(tool, source)
    policy = _matching_policy(
        permission=permission,
        workspace=workspace,
        tool=tool,
        source=source,
        principal=principal,
    )
    if policy and policy["effect"] == "deny":
        return PermissionDecision(
            False,
            False,
            {
                "success": False,
                "status": "blocked",
                "error_code": "permission_denied",
                "error_message": f"权限策略已拒绝：{permission}",
                "permission": permission,
                "policy_id": policy["id"],
                "tool": tool,
            },
        )
    if policy and policy["effect"] == "allow" and risk != "critical":
        capability = _capability(
            workspace=workspace,
            tool=tool,
            arguments=arguments,
            risk=risk,
            source=source,
            expires_at=time.time() + APPROVAL_TTL_SECONDS,
        )
        return PermissionDecision(True, True, capability={**capability, "permission": permission, "policy_id": policy["id"]})
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
            scope = _valid_scope(
                approval_scope if row["scope"] == "pending" else row["scope"],
                risk,
                conversation_id,
                task_id,
                workspace,
            )
            if scope in {"once", "task"} and row["task_id"] != task_id:
                continue
            if row["scope"] == "pending":
                if scope in {"workspace", "always", "deny"}:
                    stamp = now_iso()
                    cursor = db.execute(
                        "INSERT INTO permission_policies(permission,effect,scope,workspace,tool,source,principal,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            permission,
                            "deny" if scope == "deny" else "allow",
                            "workspace" if scope == "workspace" else "always",
                            workspace if scope == "workspace" else "",
                            tool,
                            source,
                            principal,
                            stamp,
                            stamp,
                        ),
                    )
                    policy = {"id": cursor.lastrowid}
                    db.execute(
                        "UPDATE approval_grants SET scope=?,consumed_at=? WHERE id=?",
                        (scope, now_iso(), row["id"]),
                    )
                    if scope == "deny":
                        return PermissionDecision(
                            False,
                            True,
                            {
                                "success": False,
                                "status": "blocked",
                                "error_code": "permission_denied",
                                "permission": permission,
                                "policy_id": policy["id"],
                                "tool": tool,
                            },
                        )
                    capability = json.loads(row["capabilities"] or "{}")
                    return PermissionDecision(
                        True,
                        True,
                        capability={**capability, "permission": permission, "policy_id": policy["id"]},
                    )
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
