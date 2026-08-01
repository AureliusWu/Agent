from __future__ import annotations

import sqlite3
from enum import StrEnum
from typing import Any

from app.database import now_iso


class TaskStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    PLANNING = "planning"
    READY = "ready"
    PENDING = "pending"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    WAITING_USER = "waiting_user"
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_PROVIDER = "waiting_provider"
    WAITING_PROVIDER_CREDENTIAL = "waiting_provider_credential"
    VERIFYING = "verifying"
    REPAIRING = "repairing"
    ROLLING_BACK = "rolling_back"
    RECOVERING = "recovering"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"


FINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.PARTIALLY_COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.BLOCKED,
}


RESUMABLE_TASK_STATUSES = {
    TaskStatus.WAITING_CONFIRMATION,
    TaskStatus.WAITING_PROVIDER,
    TaskStatus.WAITING_PROVIDER_CREDENTIAL,
    TaskStatus.INTERRUPTED,
    TaskStatus.TIMED_OUT,
}


_ACTIVE = {
    TaskStatus.CREATED,
    TaskStatus.QUEUED,
    TaskStatus.PLANNING,
    TaskStatus.READY,
    TaskStatus.PENDING,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_TOOL,
    TaskStatus.WAITING_USER,
    TaskStatus.WAITING_CONFIRMATION,
    TaskStatus.WAITING_PROVIDER,
    TaskStatus.WAITING_PROVIDER_CREDENTIAL,
    TaskStatus.VERIFYING,
    TaskStatus.REPAIRING,
    TaskStatus.ROLLING_BACK,
    TaskStatus.RECOVERING,
    TaskStatus.CANCEL_REQUESTED,
}


class InvalidTaskTransition(ValueError):
    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        super().__init__(f"非法任务状态转换：{current.value} -> {target.value}")
        self.current = current
        self.target = target


def can_transition(current: TaskStatus, target: TaskStatus, *, verifier: bool = False) -> bool:
    if current == target:
        return current in _ACTIVE
    if current in FINAL_TASK_STATUSES:
        return False
    if target == TaskStatus.COMPLETED:
        return verifier and (current in _ACTIVE or current in RESUMABLE_TASK_STATUSES)
    if current == TaskStatus.CREATED:
        return target in {TaskStatus.QUEUED, TaskStatus.PENDING, TaskStatus.CANCEL_REQUESTED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    if current in {TaskStatus.QUEUED, TaskStatus.PENDING}:
        return target in {
            TaskStatus.PLANNING,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_USER,
            TaskStatus.WAITING_CONFIRMATION,
            TaskStatus.WAITING_PROVIDER,
            TaskStatus.WAITING_PROVIDER_CREDENTIAL,
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
        }
    if current == TaskStatus.CANCEL_REQUESTED:
        return target in {TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.BLOCKED}
    if current == TaskStatus.ROLLING_BACK:
        return target in {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED, TaskStatus.VERIFYING}
    if current == TaskStatus.RECOVERING:
        return target in {TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.ROLLING_BACK, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    if current in RESUMABLE_TASK_STATUSES or current == TaskStatus.WAITING_USER:
        return target in {TaskStatus.RECOVERING, TaskStatus.RUNNING, TaskStatus.CANCEL_REQUESTED, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.BLOCKED}
    return target in {
        TaskStatus.RUNNING,
        TaskStatus.WAITING_TOOL,
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.WAITING_PROVIDER,
        TaskStatus.WAITING_PROVIDER_CREDENTIAL,
        TaskStatus.VERIFYING,
        TaskStatus.REPAIRING,
        TaskStatus.ROLLING_BACK,
        TaskStatus.RECOVERING,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.PARTIALLY_COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
        TaskStatus.INTERRUPTED,
        TaskStatus.BLOCKED,
    }


def require_transition(current: TaskStatus, target: TaskStatus, *, verifier: bool = False) -> None:
    if not can_transition(current, target, verifier=verifier):
        raise InvalidTaskTransition(current, target)


def record_transition(
    db: sqlite3.Connection,
    *,
    task_id: str,
    current: TaskStatus | None,
    target: TaskStatus,
    reason: str = "",
    current_step: str | None = None,
    trigger_source: str,
) -> None:
    db.execute(
        "INSERT INTO task_transitions(task_id,from_status,to_status,reason,current_step,trigger_source,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            task_id,
            current.value if current is not None else None,
            target.value,
            reason,
            current_step,
            trigger_source,
            now_iso(),
        ),
    )


def transition_task(
    db: sqlite3.Connection,
    *,
    task_id: str,
    target: TaskStatus,
    assignments: dict[str, Any] | None = None,
    expected_status: TaskStatus | None = None,
    verifier: bool = False,
    trigger_source: str,
    reason: str = "",
    lease_generation: int | None = None,
) -> TaskStatus:
    row = db.execute("SELECT status,lease_generation,current_step FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"任务不存在：{task_id}")
    current = TaskStatus(str(row["status"]))
    if expected_status is not None and current != expected_status:
        raise InvalidTaskTransition(current, target)
    require_transition(current, target, verifier=verifier)
    values = dict(assignments or {})
    step = str(values.get("current_step") or row["current_step"] or "") or None
    fields = ["status=?", "updated_at=?", *[f"{name}=?" for name in values]]
    params: list[Any] = [target.value, now_iso(), *values.values(), task_id]
    where = ["id=?"]
    if lease_generation is not None:
        where.append("lease_generation=?")
        params.append(lease_generation)
    changed = db.execute(
        f"UPDATE agent_tasks SET {', '.join(fields)} WHERE {' AND '.join(where)}",
        tuple(params),
    ).rowcount
    if changed != 1:
        raise InvalidTaskTransition(current, target)
    record_transition(
        db,
        task_id=task_id,
        current=current,
        target=target,
        reason=reason or str(values.get("termination_reason") or ""),
        current_step=step,
        trigger_source=trigger_source,
    )
    return current
