from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from .agent_profiles import require_agent_profile
from .config import settings
from .database import connect, now_iso, rows
from .schemas import ChatRequest
from .task_events import emit_task_event, latest_terminal_event
from .task_runner import interrupt_running_tasks, run_chat
from .task_state import TaskStatus


@dataclass(frozen=True)
class RuntimeJob:
    payload: ChatRequest
    api_key: str | None
    precreated: bool = True


_queue: asyncio.Queue[RuntimeJob] | None = None
_workers: list[asyncio.Task[None]] = []
_stopping = False
_conversation_locks: dict[int, asyncio.Lock] = {}
_scheduled_task_ids: set[str] = set()


def task_snapshot(task_id: str) -> dict[str, Any]:
    records = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not records:
        raise HTTPException(404, "任务不存在")
    snapshot = records[0]
    terminal = latest_terminal_event(task_id)
    if terminal:
        snapshot["result"] = terminal["payload"].get("result")
    return snapshot


def list_tasks(conversation_id: int, active_only: bool = False) -> list[dict[str, Any]]:
    params: tuple[Any, ...] = (conversation_id,)
    where = "conversation_id=?"
    if active_only:
        active = (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)
        where += " AND status IN (?,?)"
        params = (*params, *active)
    return rows(f"SELECT * FROM agent_tasks WHERE {where} ORDER BY created_at DESC LIMIT 100", params)


def _create_pending_task(payload: ChatRequest) -> None:
    conversations = rows("SELECT * FROM conversations WHERE id=?", (payload.conversation_id,))
    if not conversations:
        raise HTTPException(404, "对话不存在")
    task_id = payload.task_id or uuid.uuid4().hex
    if rows("SELECT 1 FROM agent_tasks WHERE id=?", (task_id,)):
        raise HTTPException(409, "任务 ID 已存在")
    profile = require_agent_profile(str(conversations[0].get("agent_profile_id") or "general"))
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, orchestration_mode, agent_profile_id, "
            "agent_profile_snapshot, current_phase, current_step, completed_steps, pending_steps, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                payload.conversation_id,
                TaskStatus.PENDING.value,
                payload.content,
                payload.orchestration_mode,
                profile.id,
                json.dumps(profile.catalog(), ensure_ascii=False),
                "analysis",
                "queued",
                "[]",
                "[]",
                stamp,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
            (payload.conversation_id, "user", payload.content, stamp),
        )
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (stamp, payload.conversation_id))
    emit_task_event(task_id, "task.created", {"status": TaskStatus.PENDING.value, "conversation_id": payload.conversation_id})


async def submit_task(payload: ChatRequest, api_key: str | None) -> dict[str, Any]:
    global _queue
    if _queue is None:
        raise HTTPException(503, "任务运行时尚未就绪")
    if _queue.full():
        raise HTTPException(503, "任务队列已满，请稍后重试")
    task_id = payload.task_id or uuid.uuid4().hex
    queued_payload = payload.model_copy(update={"task_id": task_id})
    _create_pending_task(queued_payload)
    try:
        _queue.put_nowait(RuntimeJob(queued_payload, api_key))
        _scheduled_task_ids.add(task_id)
    except asyncio.QueueFull as exc:
        with connect() as db:
            db.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        raise HTTPException(503, "任务队列已满，请稍后重试") from exc
    return task_snapshot(task_id)


async def resume_background_task(payload: ChatRequest, api_key: str | None) -> dict[str, Any]:
    global _queue
    if _queue is None:
        raise HTTPException(503, "任务运行时尚未就绪")
    task_id = str(payload.task_id)
    if task_id in _scheduled_task_ids:
        raise HTTPException(409, "任务已经在队列或运行中")
    try:
        _queue.put_nowait(RuntimeJob(payload, api_key, precreated=False))
        _scheduled_task_ids.add(task_id)
    except asyncio.QueueFull as exc:
        raise HTTPException(503, "任务队列已满，请稍后重试") from exc
    emit_task_event(task_id, "task.created", {"status": "resuming", "resume": True})
    return task_snapshot(task_id)


async def _worker(worker_id: int) -> None:
    assert _queue is not None
    while not _stopping:
        job = await _queue.get()
        task_id = str(job.payload.task_id)
        try:
            snapshot = task_snapshot(task_id)
            if job.precreated and snapshot["status"] != TaskStatus.PENDING.value:
                if snapshot["status"] == TaskStatus.CANCELLED.value:
                    emit_task_event(task_id, "task.cancelled", {"status": TaskStatus.CANCELLED.value})
                    continue
                raise RuntimeError(f"队列任务状态无效：{snapshot['status']}")
            lock = _conversation_locks.setdefault(job.payload.conversation_id, asyncio.Lock())
            async with lock:
                snapshot = task_snapshot(task_id)
                if job.precreated and snapshot["status"] != TaskStatus.PENDING.value:
                    if snapshot["status"] == TaskStatus.CANCELLED.value:
                        emit_task_event(task_id, "task.cancelled", {"status": TaskStatus.CANCELLED.value})
                        continue
                    raise RuntimeError(f"队列任务在等待期间状态变化：{snapshot['status']}")
                emit_task_event(task_id, "task.started", {"worker_id": worker_id})
                result = await run_chat(job.payload, job.api_key, precreated=job.precreated, event_callback=lambda event, data: emit_task_event(task_id, event, data))
            status = str(result.get("task_status") or TaskStatus.FAILED.value)
            event_type = {
                TaskStatus.COMPLETED.value: "task.completed",
                TaskStatus.PARTIALLY_COMPLETED.value: "task.completed",
                TaskStatus.CANCELLED.value: "task.cancelled",
                TaskStatus.PAUSED.value: "task.paused",
                TaskStatus.WAITING_CONFIRMATION.value: "task.paused",
                TaskStatus.INTERRUPTED.value: "task.paused",
                TaskStatus.TIMED_OUT.value: "task.paused",
            }.get(status, "task.failed" if status in {TaskStatus.FAILED.value, TaskStatus.BLOCKED.value} else "task.completed")
            emit_task_event(task_id, event_type, {"status": status, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger("agent.runtime").exception("background task %s failed", task_id)
            with connect() as db:
                db.execute(
                    "UPDATE agent_tasks SET status=?, current_step='failed', termination_reason=?, last_error=?, "
                    "updated_at=?, finished_at=? WHERE id=?",
                    (TaskStatus.FAILED.value, "后台任务执行异常", str(exc), now_iso(), now_iso(), task_id),
                )
            emit_task_event(task_id, "task.failed", {"status": TaskStatus.FAILED.value, "error": str(exc)})
        finally:
            _scheduled_task_ids.discard(task_id)
            _queue.task_done()
        if _stopping:
            return


async def start_task_runtime() -> None:
    global _queue, _workers, _stopping
    if _queue is not None:
        return
    _stopping = False
    _scheduled_task_ids.clear()
    _queue = asyncio.Queue(maxsize=max(settings.max_concurrent_tasks * 10, 10))
    _workers = [asyncio.create_task(_worker(index + 1), name=f"agent-task-worker-{index + 1}") for index in range(settings.max_concurrent_tasks)]


async def stop_task_runtime() -> None:
    global _queue, _workers, _stopping
    _stopping = True
    interrupt_running_tasks()
    for worker in _workers:
        worker.cancel()
    if _workers:
        await asyncio.gather(*_workers, return_exceptions=True)
    _workers = []
    _queue = None
