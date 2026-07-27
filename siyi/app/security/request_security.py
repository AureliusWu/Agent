from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import HTTPException

from app.config import settings
from app.database import rows
from app.deployment import deployment_requires_auth
from app.sandbox import SandboxError, workspace_root


@dataclass(frozen=True)
class ConversationScope:
    conversation_id: int
    workspace: str
    permission_mode: str


def valid_api_token(candidate: str | None) -> bool:
    expected = settings.api_token.strip()
    if not expected:
        return not deployment_requires_auth()
    return bool(candidate) and hmac.compare_digest(candidate, expected)


def require_conversation_scope(
    conversation_id: int,
    *,
    workspace: str | None = None,
    permission_mode: str | None = None,
) -> ConversationScope:
    records = rows(
        "SELECT id, workspace, permission_mode FROM conversations WHERE id=?",
        (conversation_id,),
    )
    if not records:
        raise HTTPException(404, "对话不存在")
    record = records[0]
    if workspace is not None:
        try:
            requested_workspace = str(workspace_root(workspace))
        except SandboxError as exc:
            raise HTTPException(400, str(exc)) from exc
        if requested_workspace != record["workspace"]:
            raise HTTPException(409, "请求工作区与对话授权工作区不一致")
    if permission_mode is not None and permission_mode != record["permission_mode"]:
        raise HTTPException(409, "请求权限模式与对话当前权限不一致")
    return ConversationScope(
        conversation_id=int(record["id"]),
        workspace=str(record["workspace"]),
        permission_mode=str(record["permission_mode"]),
    )


def require_task_scope(conversation_id: int, task_id: str | None) -> None:
    if task_id is None:
        return
    if not rows("SELECT 1 FROM agent_tasks WHERE id=? AND conversation_id=?", (task_id, conversation_id)):
        raise HTTPException(409, "任务不属于当前对话")
