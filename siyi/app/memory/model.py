from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.database import rows
from app.personality.identity_service import ADMINISTRATOR_ID, AGENT_ID
from app.sandbox import workspace_root


MEMORY_SCOPE_TYPES = {"user", "workspace", "conversation", "task"}


def normalize_memory_content(content: str) -> str:
    return " ".join(content.strip().casefold().split())


def memory_content_fingerprint(content: str) -> str:
    return hashlib.sha256(normalize_memory_content(content).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoryScope:
    scope_type: str
    scope_id: str
    workspace: str
    namespace: str
    agent_id: str = AGENT_ID


def resolve_memory_scope(
    workspace: str,
    *,
    namespace: str = "project",
    scope_type: str | None = None,
    conversation_id: int | None = None,
    task_id: str | None = None,
) -> MemoryScope:
    """Resolve and authorize one Memory v2 scope against persisted ownership."""

    resolved_type = scope_type or ("user" if namespace == "personal" else "workspace")
    if resolved_type not in MEMORY_SCOPE_TYPES:
        raise ValueError("记忆范围必须是 user、workspace、conversation 或 task")
    if namespace not in {"project", "personal"}:
        raise ValueError("记忆命名空间必须是 project 或 personal")
    if resolved_type == "user":
        if namespace != "personal":
            raise ValueError("user 范围必须使用 personal 命名空间")
        if conversation_id is not None or task_id is not None:
            raise ValueError("user 范围不能绑定会话或任务")
        return MemoryScope("user", ADMINISTRATOR_ID, "", "personal")
    if namespace != "project":
        raise ValueError("项目、会话和任务记忆必须使用 project 命名空间")
    if not workspace.strip():
        raise ValueError("项目记忆需要先选择工作区")
    root = str(workspace_root(workspace))
    if resolved_type == "workspace":
        if conversation_id is not None or task_id is not None:
            raise ValueError("workspace 范围不能绑定会话或任务")
        return MemoryScope("workspace", root, root, "project")
    if resolved_type == "conversation":
        if conversation_id is None or task_id is not None:
            raise ValueError("conversation 范围需要唯一的 conversation_id")
        records = rows("SELECT workspace FROM conversations WHERE id=? AND agent_id=?", (conversation_id, AGENT_ID))
        if not records or str(workspace_root(str(records[0]["workspace"]))) != root:
            raise ValueError("会话不属于当前工作区")
        return MemoryScope("conversation", str(conversation_id), root, "project")
    if not task_id or conversation_id is not None:
        raise ValueError("task 范围需要唯一的 task_id")
    records = rows(
        "SELECT c.workspace FROM agent_tasks t JOIN conversations c ON c.id=t.conversation_id "
        "WHERE t.id=? AND t.agent_id=?",
        (task_id, AGENT_ID),
    )
    if not records or str(workspace_root(str(records[0]["workspace"]))) != root:
        raise ValueError("任务不属于当前工作区")
    return MemoryScope("task", task_id, root, "project")
