"""A single non-serializable delegate call authorized by its parent tool."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from app.database import rows
from app.permissions import PermissionDecision, permission_denial
from app.tools.registry import REGISTRY


def _digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def issue_delegate_permission(*, decision: PermissionDecision, workspace: str, mode: str,
    conversation_id: int | None, task_id: str | None, parent_tool: str,
    parent_source: str, parent_risk: str, delegate: str, arguments: dict[str, Any]):
    """Return a one-shot closure, not an HTTP token or a permission-mode override."""
    if not decision.allowed:
        raise PermissionError("A denied parent cannot delegate execution")
    spec = REGISTRY[delegate]
    if spec.risk == "critical":
        raise PermissionError("Extensions cannot delegate critical tools")
    bound_workspace = Path(workspace).resolve()
    arguments_hash = _digest(arguments)
    expires_at = time.monotonic() + 120
    used = False
    authority = (rows("SELECT workspace,permission_mode FROM conversations WHERE id=?", (conversation_id,))
                 if conversation_id is not None else [])
    persisted = bool(authority)
    authoritative_mode = str(authority[0]["permission_mode"]) if authority else mode
    # An effective ask mode may be a runtime security tightening. Its grant
    # still belongs to the unchanged authoritative conversation snapshot.
    compatible_authority = (
        (mode == authoritative_mode or (mode == "ask" and authoritative_mode in {"agent", "full"}))
        and (not authority or Path(authority[0]["workspace"]).resolve() == bound_workspace)
    )

    def denied() -> PermissionDecision:
        return PermissionDecision(False, False, {"success": False, "status": "blocked",
            "error_code": "delegation_grant_invalid", "error_message": "委派授权失效或上下文不匹配"})

    def authorize_delegate(**values: Any) -> PermissionDecision:
        nonlocal used
        if (used or not compatible_authority or time.monotonic() >= expires_at or values.get("mode") != mode
                or values.get("conversation_id") != conversation_id or values.get("task_id") != task_id
                or Path(str(values.get("workspace") or "")).resolve() != bound_workspace
                or values.get("tool") != delegate or values.get("risk") != spec.risk
                or _digest(values.get("arguments") or {}) != arguments_hash):
            return denied()
        if conversation_id is not None:
            current = rows("SELECT workspace,permission_mode FROM conversations WHERE id=?", (conversation_id,))
            if bool(current) != persisted:
                return denied()
            if current and (current[0]["permission_mode"] != authoritative_mode
                    or Path(current[0]["workspace"]).resolve() != bound_workspace):
                return denied()
        rejection = permission_denial(mode=mode, risk=parent_risk, tool=parent_tool,
            source=parent_source, workspace=workspace)
        if rejection is None:
            rejection = permission_denial(mode=mode, risk=spec.risk, tool=delegate, workspace=workspace)
        if rejection:
            return rejection
        used = True
        return PermissionDecision(True, decision.confirmed)

    return authorize_delegate
