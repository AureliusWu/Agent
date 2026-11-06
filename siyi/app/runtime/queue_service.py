from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from app.database import connect, now_iso, rows
from app.config import settings
from app.runtime.task_events import emit_task_event
from app.runtime.task_leases import RUNTIME_INSTANCE_ID


QueueKind = Literal["submit", "resume", "steer", "system"]
QueuePriority = Literal["now", "next", "later"]
QueueStatus = Literal["pending", "claimed", "consumed", "cancelled"]

PRIORITY_VALUES: dict[QueuePriority, int] = {"now": 0, "next": 10, "later": 20}
STEERING_MESSAGE_ID = "steering_message_id"


class SteeringAdmissionConflict(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
    with connect() as db:
        return enqueue_in_transaction(
            db,
            conversation_id=conversation_id,
            task_id=task_id,
            kind=kind,
            content=content,
            payload=payload,
            priority=priority,
            target_scope=target_scope,
            target_agent_id=target_agent_id,
        )


def enqueue_in_transaction(
    db: sqlite3.Connection,
    *,
    conversation_id: int,
    kind: QueueKind,
    content: str,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
    priority: QueuePriority = "later",
    target_scope: str = "conversation",
    target_agent_id: str | None = None,
    item_id: str | None = None,
) -> QueueItem:
    """Insert a queue item in the caller's transaction, without committing it."""
    item_id = item_id or uuid.uuid4().hex
    stamp = now_iso()
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
    record = db.execute("SELECT * FROM conversation_queue_items WHERE id=?", (item_id,)).fetchone()
    return QueueItem.from_row(dict(record))


def admit_steering(
    task_id: str, *, content: str, priority: QueuePriority, target_scope: str,
    target_agent_id: str | None, request_id: uuid.UUID | None,
) -> QueueItem:
    """Commit the queue, history mirror and acknowledgement event together."""
    if target_scope != "task" or target_agent_id is not None:
        raise SteeringAdmissionConflict("unsupported_steering_scope", "当前仅支持引导主任务，不支持指定子代理")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if request_id is not None:
            prior = db.execute("SELECT * FROM conversation_queue_items WHERE id=?", (request_id.hex,)).fetchone()
            if prior is not None:
                item = QueueItem.from_row(dict(prior))
                # Promotions may change visible queue priority; retry the
                # original request, not a later queue control operation.
                if (item.kind == "steer" and item.task_id == task_id and item.content == content
                    and item.target_scope == target_scope and item.target_agent_id == target_agent_id
                    and item.payload.get("steering_request_priority") == priority):
                    return item
                raise SteeringAdmissionConflict("steering_request_conflict", "引导请求标识已用于其他请求")
        task = db.execute("SELECT conversation_id,status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(task_id)
        if task["status"] != "running":
            raise SteeringAdmissionConflict("steering_task_not_running", "只有运行中的任务可以接收引导消息")
        conversation_id = int(task["conversation_id"])
        stamp = now_iso()
        message = db.execute(
            "INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
            (conversation_id, "user", content, task_id, stamp),
        )
        item = enqueue_in_transaction(
            db, conversation_id=conversation_id, task_id=task_id, kind="steer", content=content,
            payload={STEERING_MESSAGE_ID: int(message.lastrowid), "steering_request_priority": priority},
            priority=priority, target_scope=target_scope, target_agent_id=target_agent_id,
            item_id=request_id.hex if request_id is not None else None,
        )
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (stamp, conversation_id))
        event = emit_task_event(task_id, "queue.updated", {"operation": "steer", "queue_item": item.__dict__}, db=db)
        if not event["id"]:
            raise SteeringAdmissionConflict("steering_lease_changed", "任务运行权已变更，未接收引导消息")
    return item


def pending_submit_count_in_transaction(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM conversation_queue_items WHERE status='pending' AND kind='submit'").fetchone()[0])


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
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            "UPDATE conversation_queue_items SET status='cancelled',consumed_at=?,updated_at=? "
            "WHERE id=? AND status IN ('pending','claimed')",
            (stamp, stamp, item_id),
        ).rowcount
        if not changed:
            raise ValueError("队列项已完成或已取消")
        record = db.execute("SELECT * FROM conversation_queue_items WHERE id=?", (item_id,)).fetchone()
        item = QueueItem.from_row(dict(record))
        if item.kind == "steer" and item.task_id is not None:
            message_id = item.payload.get(STEERING_MESSAGE_ID)
            if type(message_id) is int and message_id > 0:
                db.execute(
                    "DELETE FROM messages WHERE id=? AND conversation_id=? AND task_id=? AND role='user' AND content=?",
                    (message_id, item.conversation_id, item.task_id, item.content),
                )
            # Older rows have no stable mirror ID. Never guess by text; the
            # model history query fences those ambiguous cancelled mirrors.
    return item


def pending_steering(task_id: str) -> list[QueueItem]:
    return [QueueItem.from_row(record) for record in rows(
        "SELECT * FROM conversation_queue_items WHERE task_id=? AND kind='steer' AND status='pending' "
        "ORDER BY priority_value ASC, created_at ASC, rowid ASC",
        (task_id,),
    )]


def legacy_consumed_steering(task_id: str) -> list[QueueItem]:
    """Recover consumed workspace-free guidance from before durable checkpoints."""
    return [QueueItem.from_row(record) for record in rows(
        "SELECT * FROM conversation_queue_items WHERE task_id=? AND kind='steer' AND status='consumed' "
        "ORDER BY consumed_at ASC, rowid ASC",
        (task_id,),
    )]


def consume_steering_at_safe_point(task_id: str) -> list[QueueItem]:
    consumed: list[QueueItem] = []
    for record in pending_steering(task_id):
        item = claim(record.id)
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
