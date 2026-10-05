from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from app.database import connect, now_iso, rows
from app.config import settings
from app.runtime.task_leases import RUNTIME_INSTANCE_ID


QueueKind = Literal["submit", "resume", "steer", "system"]
QueuePriority = Literal["now", "next", "later"]
QueueStatus = Literal["pending", "claimed", "consumed", "cancelled"]

PRIORITY_VALUES: dict[QueuePriority, int] = {"now": 0, "next": 10, "later": 20}


@dataclass(frozen=True)
class QueueItem:
    id: str
    conversation_id: int
    task_id: str | None
    kind: QueueKind
    priority: QueuePriority
    status: QueueStatus
    content: str
    payload: dict[str, Any]
    target_scope: str
    target_agent_id: str | None
    created_at: str
    claim_owner_instance_id: str | None = None
    claim_generation: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "QueueItem":
        return cls(
            id=str(row["id"]),
            conversation_id=int(row["conversation_id"]),
            task_id=str(row["task_id"]) if row.get("task_id") else None,
            kind=str(row["kind"]),  # type: ignore[arg-type]
            priority=str(row["priority"]),  # type: ignore[arg-type]
            status=str(row["status"]),  # type: ignore[arg-type]
            content=str(row.get("content") or ""),
            payload=json.loads(row.get("payload_json") or "{}"),
            target_scope=str(row.get("target_scope") or "conversation"),
            target_agent_id=str(row["target_agent_id"]) if row.get("target_agent_id") else None,
            created_at=str(row["created_at"]),
            claim_owner_instance_id=str(row["claim_owner_instance_id"]) if row.get("claim_owner_instance_id") else None,
            claim_generation=int(row.get("claim_generation") or 0),
        )


def enqueue(
    *,
    conversation_id: int,
    kind: QueueKind,
    content: str,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
    priority: QueuePriority = "later",
    target_scope: str = "conversation",
    target_agent_id: str | None = None,
) -> QueueItem:
    item_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversation_queue_items("
            "id,conversation_id,task_id,kind,priority,priority_value,status,content,payload_json,"
            "target_scope,target_agent_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item_id,
                conversation_id,
                task_id,
                kind,
                priority,
                PRIORITY_VALUES[priority],
                "pending",
                content,
                json.dumps(payload or {}, ensure_ascii=False),
                target_scope,
                target_agent_id,
                stamp,
                stamp,
            ),
        )
    return get_item(item_id)


def get_item(item_id: str) -> QueueItem:
    records = rows("SELECT * FROM conversation_queue_items WHERE id=?", (item_id,))
    if not records:
        raise KeyError(item_id)
    return QueueItem.from_row(records[0])


def pending_items(*, conversation_id: int | None = None, kind: QueueKind | None = None) -> list[QueueItem]:
    clauses = ["status='pending'"]
    params: list[Any] = []
    if conversation_id is not None:
        clauses.append("conversation_id=?")
        params.append(conversation_id)
    if kind is not None:
        clauses.append("kind=?")
        params.append(kind)
    # UUIDs identify items, not arrival order. Use this ordinary SQLite
    # table's insertion order when timestamps collide, without changing priority.
    query = (
        "SELECT * FROM conversation_queue_items WHERE "
        + " AND ".join(clauses)
        + " ORDER BY priority_value ASC, created_at ASC, rowid ASC"
    )
    return [QueueItem.from_row(item) for item in rows(query, tuple(params))]


def claim(item_id: str) -> QueueItem | None:
    stamp = now_iso()
    expires_at = time.time() + max(int(settings.task_lease_seconds), 30)
    with connect() as db:
        changed = db.execute(
            "UPDATE conversation_queue_items SET status='claimed',claimed_at=?,updated_at=?,"
            "claim_owner_instance_id=?,claim_owner_pid=?,claim_generation=claim_generation+1,claim_expires_at=? "
            "WHERE id=? AND status='pending'",
            (stamp, stamp, RUNTIME_INSTANCE_ID, os.getpid(), expires_at, item_id),
        ).rowcount
    return get_item(item_id) if changed else None


def finish(item_id: str, status: Literal["consumed", "cancelled"] = "consumed") -> QueueItem:
    stamp = now_iso()
    with connect() as db:
        changed = db.execute(
            "UPDATE conversation_queue_items SET status=?,consumed_at=?,updated_at=?,claim_expires_at=NULL "
            "WHERE id=? AND (status='pending' OR (status='claimed' AND (claim_owner_instance_id=? OR claim_owner_instance_id IS NULL)))",
            (status, stamp, stamp, item_id, RUNTIME_INSTANCE_ID),
        ).rowcount
    if not changed:
        raise ValueError("队列项由另一运行实例持有或已结束")
    return get_item(item_id)


def promote(item_id: str, priority: QueuePriority = "now") -> QueueItem:
    stamp = now_iso()
    with connect() as db:
        changed = db.execute(
            "UPDATE conversation_queue_items SET priority=?,priority_value=?,updated_at=? "
            "WHERE id=? AND status='pending'",
            (priority, PRIORITY_VALUES[priority], stamp, item_id),
        ).rowcount
    if not changed:
        raise ValueError("只有尚未执行的队列项可以提升优先级")
    return get_item(item_id)


def cancel(item_id: str) -> QueueItem:
    stamp = now_iso()
    with connect() as db:
        changed = db.execute(
            "UPDATE conversation_queue_items SET status='cancelled',consumed_at=?,updated_at=? "
            "WHERE id=? AND status IN ('pending','claimed')",
            (stamp, stamp, item_id),
        ).rowcount
    if not changed:
        raise ValueError("队列项已完成或已取消")
    return get_item(item_id)


def consume_steering_at_safe_point(task_id: str) -> list[QueueItem]:
    records = rows(
        "SELECT * FROM conversation_queue_items WHERE task_id=? AND kind='steer' AND status='pending' "
        "ORDER BY priority_value ASC, created_at ASC, rowid ASC",
        (task_id,),
    )
    consumed: list[QueueItem] = []
    for record in records:
        item = claim(str(record["id"]))
        if item is None:
            continue
        consumed.append(finish(item.id))
    return consumed


def recover_claimed_items() -> int:
    stamp = now_iso()
    with connect() as db:
        return db.execute(
            "UPDATE conversation_queue_items SET status='pending',claimed_at=NULL,updated_at=?,"
            "claim_owner_instance_id=NULL,claim_owner_pid=NULL,claim_expires_at=NULL "
            "WHERE status='claimed' AND (claim_expires_at IS NULL OR claim_expires_at<=?) "
            "AND NOT EXISTS (SELECT 1 FROM task_leases l WHERE l.task_id=conversation_queue_items.task_id "
            "AND l.status='active' AND l.expires_at>?)",
            (stamp, time.time(), time.time()),
        ).rowcount
