"""Metadata-only read model for tasks that need the desktop operator."""

from __future__ import annotations

from typing import Literal, TypedDict

from app.database import rows
from app.runtime.task_state import TaskStatus


AttentionCategory = Literal["confirmation", "user_input", "credential", "provider", "recovery"]
_CATEGORIES: dict[TaskStatus, AttentionCategory] = {
    TaskStatus.WAITING_CONFIRMATION: "confirmation",
    TaskStatus.WAITING_USER: "user_input",
    TaskStatus.WAITING_PROVIDER_CREDENTIAL: "credential",
    TaskStatus.WAITING_PROVIDER: "provider",
    TaskStatus.INTERRUPTED: "recovery",
    TaskStatus.TIMED_OUT: "recovery",
}
_PRIORITIES = {category: rank for rank, category in enumerate(dict.fromkeys(_CATEGORIES.values()))}
ATTENTION_DEFAULT_LIMIT = 50
ATTENTION_MAX_LIMIT = 100


class AttentionItem(TypedDict):
    task_id: str
    conversation_id: int
    status: TaskStatus
    category: AttentionCategory
    updated_at: str


class AttentionPage(TypedDict):
    items: list[AttentionItem]
    total: int


def list_task_attention(*, conversation_id: int | None = None, limit: int = ATTENTION_DEFAULT_LIMIT) -> AttentionPage:
    """Project authoritative actionable states without loading task content.

    A single SELECT gives the count and bounded page the same SQLite snapshot.
    Being listed is not a grant, nor a promise that any old checkpoint can resume.
    """
    if not 1 <= limit <= ATTENTION_MAX_LIMIT:
        raise ValueError("attention limit outside allowed bounds")
    statuses = tuple(status.value for status in _CATEGORIES)
    placeholders = ",".join("?" for _ in statuses)
    where = f"resumable=1 AND status IN ({placeholders})"
    scope = ()
    if conversation_id is not None:
        where += " AND conversation_id=?"
        scope = (conversation_id,)
    order = " ".join(f"WHEN ? THEN {_PRIORITIES[category]}" for category in _CATEGORIES.values())
    records = rows(
        "SELECT id AS task_id, conversation_id, status, updated_at, COUNT(*) OVER () AS total "
        f"FROM agent_tasks WHERE {where} "
        f"ORDER BY CASE status {order} END, updated_at DESC, id DESC LIMIT ?",
        (*statuses, *scope, *statuses, limit),
    )
    items: list[AttentionItem] = []
    for record in records:
        status = TaskStatus(record["status"])
        items.append({
            "task_id": record["task_id"],
            "conversation_id": record["conversation_id"],
            "status": status,
            "category": _CATEGORIES[status],
            "updated_at": record["updated_at"],
        })
    return {"items": items, "total": int(records[0]["total"]) if records else 0}
