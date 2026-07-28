from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app.personality.agent_profiles import require_agent_profile
from app.config import settings
from app.database import connect, now_iso, rows
from app.runtime.queue_service import QueueItem, claim, enqueue, finish, get_item, pending_items, recover_claimed_items
from app.schemas import ChatRequest
from app.runtime.task_events import emit_task_event, latest_terminal_event
from app.runtime.runner import credential_binding, interrupt_running_tasks, run_chat
from app.runtime.task_leases import TaskLeaseConflict
from app.runtime.task_state import FINAL_TASK_STATUSES, RESUMABLE_TASK_STATUSES, TaskStatus
from app.cognition.planning import load_task_plan
from app.providers.registry import provider_profile


_queue: asyncio.Queue[str] | None = None
_workers: list[asyncio.Task[None]] = []
_stopping = False
_conversation_locks: dict[int, asyncio.Lock] = {}
_scheduled_task_ids: set[str] = set()
_ephemeral_credentials: dict[str, dict[str, str | None]] = {}


@dataclass(frozen=True)
class ActiveRunControl:
    conversation_id: int
    state: str
    task_id: str | None = None
    queue_item_id: str | None = None


_active_runs: dict[int, ActiveRunControl] = {}


def conversation_runtime_state(conversation_id: int) -> dict[str, Any]:
    control = _active_runs.get(conversation_id)
    if control is None:
        return {"conversation_id": conversation_id, "state": "idle", "task_id": None, "queue_item_id": None}
    return {
        "conversation_id": control.conversation_id,
        "state": control.state,
        "task_id": control.task_id,
        "queue_item_id": control.queue_item_id,
    }


def task_snapshot(task_id: str, *, include_contract: bool = True) -> dict[str, Any]:
    records = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not records:
        raise HTTPException(404, "任务不存在")
    snapshot = records[0]
    if include_contract:
        contract = load_task_plan(task_id)
        snapshot["task_contract"] = contract.as_dict() if contract else None
    terminal = latest_terminal_event(task_id)
    if terminal:
        snapshot["result"] = terminal["payload"].get("result")
    elif snapshot["status"] in {status.value for status in FINAL_TASK_STATUSES}:
        messages = rows(
            "SELECT content,reasoning_content FROM messages WHERE task_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        if messages:
            snapshot["result"] = {
                "content": messages[0]["content"],
                "reasoning": messages[0].get("reasoning_content"),
                "pending_actions": [],
                "task_id": task_id,
                "task_status": snapshot["status"],
            }
    return snapshot


def list_tasks(conversation_id: int, active_only: bool = False) -> list[dict[str, Any]]:
    params: tuple[Any, ...] = (conversation_id,)
    where = "conversation_id=?"
    if active_only:
        active = (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)
        where += " AND status IN (?,?)"
        params = (*params, *active)
    return rows(f"SELECT * FROM agent_tasks WHERE {where} ORDER BY created_at DESC LIMIT 100", params)


def _create_pending_task(payload: ChatRequest, api_key: str | None, search_credentials: dict[str, str] | None) -> None:
    conversations = rows("SELECT * FROM conversations WHERE id=?", (payload.conversation_id,))
    if not conversations:
        raise HTTPException(404, "对话不存在")
    task_id = payload.task_id or uuid.uuid4().hex
    if rows("SELECT 1 FROM agent_tasks WHERE id=?", (task_id,)):
        raise HTTPException(409, "任务 ID 已存在")
    profile = require_agent_profile("general")
    binding = credential_binding(api_key, search_credentials)
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, orchestration_mode, agent_profile_id, "
            "agent_profile_snapshot, provider_profile_snapshot, credential_source, credential_profile_id, required_capabilities, credential_binding_hash, "
            "current_phase, current_step, completed_steps, pending_steps, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                payload.conversation_id,
                TaskStatus.PENDING.value,
                payload.content,
                "single",
                profile.id,
                json.dumps(profile.catalog(), ensure_ascii=False),
                json.dumps(provider_profile(), ensure_ascii=False, sort_keys=True),
                binding["source"],
                binding["profile_id"],
                json.dumps(binding["required_capabilities"], ensure_ascii=False),
                binding["binding_hash"],
                "analysis",
                "queued",
                "[]",
                "[]",
                stamp,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO messages(conversation_id, role, content, task_id, created_at) VALUES(?,?,?,?,?)",
            (payload.conversation_id, "user", payload.content, task_id, stamp),
        )
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (stamp, payload.conversation_id))
    emit_task_event(task_id, "task.created", {"status": TaskStatus.PENDING.value, "conversation_id": payload.conversation_id})


async def submit_task(payload: ChatRequest, api_key: str | None, search_credentials: dict[str, str] | None = None) -> dict[str, Any]:
    global _queue
    if _queue is None:
        raise HTTPException(503, "任务运行时尚未就绪")
    if len(pending_items(kind="submit")) >= max(settings.max_concurrent_tasks * 10, 10):
        raise HTTPException(503, "任务队列已满，请稍后重试")
    task_id = payload.task_id or uuid.uuid4().hex
    queued_payload = payload.model_copy(update={"task_id": task_id})
    _create_pending_task(queued_payload, api_key, search_credentials)
    try:
        item = enqueue(
            conversation_id=queued_payload.conversation_id,
            task_id=task_id,
            kind="submit",
            content=queued_payload.content,
            payload=queued_payload.model_dump(mode="json"),
            priority="later",
        )
        _ephemeral_credentials[task_id] = {"model": api_key, **(search_credentials or {})}
        _queue.put_nowait(item.id)
        _scheduled_task_ids.add(task_id)
    except Exception as exc:
        with connect() as db:
            db.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        raise HTTPException(503, "任务入队失败，请稍后重试") from exc
    return {**task_snapshot(task_id, include_contract=False), "queue_item": item.__dict__}


async def resume_background_task(payload: ChatRequest, api_key: str | None, search_credentials: dict[str, str] | None = None) -> dict[str, Any]:
    global _queue
    if _queue is None:
        raise HTTPException(503, "任务运行时尚未就绪")
    task_id = str(payload.task_id)
    if task_id in _scheduled_task_ids:
        raise HTTPException(409, "任务已经在队列或运行中")
    item = enqueue(
        conversation_id=payload.conversation_id,
        task_id=task_id,
        kind="resume",
        content=payload.content,
        payload=payload.model_dump(mode="json"),
        priority="next",
    )
    _ephemeral_credentials[task_id] = {"model": api_key, **(search_credentials or {})}
    _queue.put_nowait(item.id)
    _scheduled_task_ids.add(task_id)
    emit_task_event(task_id, "task.created", {"status": "resuming", "resume": True})
    return {**task_snapshot(task_id, include_contract=False), "queue_item": item.__dict__}


async def _worker(worker_id: int) -> None:
    assert _queue is not None
    while not _stopping:
        item_id = await _queue.get()
        item: QueueItem | None = None
        task_id = ""
        try:
            candidates = [candidate for candidate in pending_items() if candidate.kind in {"submit", "resume"}]
            if not candidates:
                continue
            pending = candidates[0]
            item_id = pending.id
            lock = _conversation_locks.setdefault(pending.conversation_id, asyncio.Lock())
            if lock.locked():
                await asyncio.sleep(0.1)
                _queue.put_nowait(item_id)
                continue
            item = claim(item_id)
            if item is None:
                continue
            _active_runs[item.conversation_id] = ActiveRunControl(
                conversation_id=item.conversation_id,
                state="dispatching",
                task_id=item.task_id,
                queue_item_id=item.id,
            )
            payload = ChatRequest.model_validate(item.payload)
            task_id = str(payload.task_id)
            precreated = item.kind == "submit"
            snapshot = task_snapshot(task_id, include_contract=False)
            if precreated and snapshot["status"] != TaskStatus.PENDING.value:
                if snapshot["status"] == TaskStatus.CANCELLED.value:
                    emit_task_event(task_id, "task.cancelled", {"status": TaskStatus.CANCELLED.value})
                elif snapshot["status"] in {status.value for status in RESUMABLE_TASK_STATUSES}:
                    emit_task_event(task_id, "task.interrupted", {"status": snapshot["status"]})
                else:
                    logging.getLogger("agent.runtime").warning(
                        "skipping queued task %s after status changed to %s",
                        task_id,
                        snapshot["status"],
                    )
                continue
            async with lock:
                _active_runs[item.conversation_id] = ActiveRunControl(
                    conversation_id=item.conversation_id,
                    state="running",
                    task_id=task_id,
                    queue_item_id=item.id,
                )
                snapshot = task_snapshot(task_id, include_contract=False)
                if precreated and snapshot["status"] != TaskStatus.PENDING.value:
                    if snapshot["status"] == TaskStatus.CANCELLED.value:
                        emit_task_event(task_id, "task.cancelled", {"status": TaskStatus.CANCELLED.value})
                    elif snapshot["status"] in {status.value for status in RESUMABLE_TASK_STATUSES}:
                        emit_task_event(task_id, "task.interrupted", {"status": snapshot["status"]})
                    else:
                        logging.getLogger("agent.runtime").warning(
                            "skipping queued task %s after status changed to %s while waiting",
                            task_id,
                            snapshot["status"],
                        )
                    continue
                emit_task_event(task_id, "task.started", {"worker_id": worker_id})
                credentials = _ephemeral_credentials.get(task_id) or {}
                result = await run_chat(
                    payload,
                    credentials.get("model"),
                    precreated=precreated,
                    event_callback=lambda event, data: emit_task_event(task_id, event, data),
                    search_credentials={key: str(value) for key, value in credentials.items() if key in {"tavily", "brave"} and value},
                )
            status = str(result.get("task_status") or TaskStatus.FAILED.value)
            event_type = {
                TaskStatus.COMPLETED.value: "task.completed",
                TaskStatus.PARTIALLY_COMPLETED.value: "task.completed",
                TaskStatus.CANCELLED.value: "task.cancelled",
                TaskStatus.PAUSED.value: "task.interrupted",
                TaskStatus.WAITING_CONFIRMATION.value: "task.interrupted",
                TaskStatus.WAITING_PROVIDER.value: "task.interrupted",
                TaskStatus.WAITING_PROVIDER_CREDENTIAL.value: "task.interrupted",
                TaskStatus.INTERRUPTED.value: "task.interrupted",
                TaskStatus.TIMED_OUT.value: "task.interrupted",
            }.get(status, "task.failed" if status in {TaskStatus.FAILED.value, TaskStatus.BLOCKED.value} else "task.completed")
            emit_task_event(task_id, event_type, {"status": status, "result": result})
        except asyncio.CancelledError:
            raise
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if exc.status_code == 409 and detail.get("code") == "owned_by_other_runtime":
                logging.getLogger("agent.runtime").info(
                    "background task %s was already owned; duplicate queue execution discarded",
                    task_id,
                )
            else:
                logging.getLogger("agent.runtime").exception("background task %s failed with HTTP %s", task_id, exc.status_code)
                with connect() as db:
                    db.execute(
                        "UPDATE agent_tasks SET status=?, current_step='failed', termination_reason=?, last_error=?, "
                        "updated_at=?, finished_at=? WHERE id=? AND NOT EXISTS ("
                        "SELECT 1 FROM task_leases l WHERE l.task_id=agent_tasks.id AND l.status='active' AND l.expires_at>?)",
                        (TaskStatus.FAILED.value, "后台任务执行异常", str(exc.detail), now_iso(), now_iso(), task_id, time.time()),
                    )
                emit_task_event(task_id, "task.failed", {"status": TaskStatus.FAILED.value, "error": str(exc.detail)})
        except TaskLeaseConflict:
            logging.getLogger("agent.runtime").warning(
                "background task %s lost lease ownership; stale state write suppressed",
                task_id,
            )
        except Exception as exc:
            logging.getLogger("agent.runtime").exception("background task %s failed", task_id)
            with connect() as db:
                db.execute(
                    "UPDATE agent_tasks SET status=?, current_step='failed', termination_reason=?, last_error=?, "
                    "updated_at=?, finished_at=? WHERE id=? AND NOT EXISTS ("
                    "SELECT 1 FROM task_leases l WHERE l.task_id=agent_tasks.id AND l.status='active' AND l.expires_at>?)",
                    (TaskStatus.FAILED.value, "后台任务执行异常", str(exc), now_iso(), now_iso(), task_id, time.time()),
                )
            emit_task_event(task_id, "task.failed", {"status": TaskStatus.FAILED.value, "error": str(exc)})
        finally:
            if item is not None:
                current = _active_runs.get(item.conversation_id)
                if current and current.queue_item_id == item.id:
                    _active_runs.pop(item.conversation_id, None)
            if item is not None and item.status == "claimed":
                try:
                    finish(item.id)
                except (KeyError, ValueError):
                    pass
            if task_id:
                _scheduled_task_ids.discard(task_id)
            if task_id:
                _ephemeral_credentials.pop(task_id, None)
            _queue.task_done()
        if _stopping:
            return


async def start_task_runtime() -> None:
    global _queue, _workers, _stopping
    if _queue is not None:
        return
    _stopping = False
    from app.process_supervisor import recover_orphaned_processes

    recover_orphaned_processes()
    _scheduled_task_ids.clear()
    recover_claimed_items()
    _queue = asyncio.Queue()
    for item in pending_items():
        if item.kind in {"submit", "resume"}:
            _queue.put_nowait(item.id)
            if item.task_id:
                _scheduled_task_ids.add(item.task_id)
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
    _ephemeral_credentials.clear()
