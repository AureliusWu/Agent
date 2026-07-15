import asyncio
import json

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..database import rows
from ..multi_agent import task_agent_trace
from ..recovery import list_checkpoints
from ..schemas import ChatRequest, TaskResumeRequest
from ..task_runner import cancel_task, pause_task, run_chat
from ..task_events import TERMINAL_EVENT_TYPES, task_events
from ..task_runtime import list_tasks, resume_background_task, submit_task, task_snapshot
from ..task_state import FINAL_TASK_STATUSES, RESUMABLE_TASK_STATUSES

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat")
async def chat(payload: ChatRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    return await run_chat(payload, x_model_api_key)


@router.post("/tasks", status_code=status.HTTP_202_ACCEPTED)
async def create_task(payload: ChatRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    return await submit_task(payload, x_model_api_key)


@router.get("/tasks")
async def tasks(conversation_id: int = Query(...), active: bool = Query(default=False)) -> list[dict]:
    return list_tasks(conversation_id, active_only=active)


@router.get("/tasks/recoverable")
async def recoverable_tasks(conversation_id: int | None = Query(default=None)) -> list[dict]:
    statuses = tuple(status.value for status in RESUMABLE_TASK_STATUSES)
    placeholders = ",".join("?" for _ in statuses)
    params: tuple = statuses
    where = f"status IN ({placeholders}) AND resumable=1"
    if conversation_id is not None:
        where += " AND conversation_id=?"
        params = (*params, conversation_id)
    tasks = rows(f"SELECT * FROM agent_tasks WHERE {where} ORDER BY updated_at DESC", params)
    for task in tasks:
        task["checkpoints"] = list_checkpoints(task["id"])
    return tasks


@router.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    return task_snapshot(task_id)


@router.get("/tasks/{task_id}/events")
async def stream_task_events(
    task_id: str,
    request: Request,
    after_id: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    task_snapshot(task_id, include_contract=False)
    cursor = after_id
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))

    async def event_stream():
        nonlocal cursor
        idle_polls = 0
        while not await request.is_disconnected():
            available = task_events(task_id, cursor)
            if available:
                idle_polls = 0
                for event in available:
                    cursor = int(event["id"])
                    data = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['event']}\ndata: {data}\n\n"
                    if event["event"] in TERMINAL_EVENT_TYPES:
                        return
            else:
                idle_polls += 1
                snapshot = task_snapshot(task_id, include_contract=False)
                final_statuses = {item.value for item in FINAL_TASK_STATUSES}
                if snapshot["status"] in final_statuses and idle_polls >= 2:
                    return
                if idle_polls % 40 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/tasks/{task_id}/cancel")
async def stop_task(task_id: str) -> dict:
    return cancel_task(task_id)


@router.post("/tasks/{task_id}/pause")
async def pause_running_task(task_id: str) -> dict:
    return await pause_task(task_id)


@router.post("/tasks/{task_id}/abandon")
async def abandon_task(task_id: str) -> dict:
    return cancel_task(task_id)


@router.post("/tasks/{task_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_task(task_id: str, payload: TaskResumeRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    tasks = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not tasks:
        raise HTTPException(404, "任务不存在")
    task = tasks[0]
    return await resume_background_task(
        ChatRequest(
            conversation_id=task["conversation_id"],
            content=task["prompt"],
            task_id=task_id,
            approved_actions=payload.approved_actions,
            approval_scope=payload.approval_scope,
            resume=True,
            checkpoint_sequence=payload.checkpoint_sequence,
            allow_workspace_drift=payload.allow_workspace_drift,
            retry_uncertain=payload.retry_uncertain,
            orchestration_mode=task.get("orchestration_mode") or "single",
            agent_count=max(1, int(task.get("child_agent_count") or 1)),
        ),
        x_model_api_key,
    )


@router.get("/tasks/{task_id}/checkpoints")
async def task_checkpoints(task_id: str) -> list[dict]:
    if not rows("SELECT id FROM agent_tasks WHERE id=?", (task_id,)):
        raise HTTPException(404, "任务不存在")
    return list_checkpoints(task_id)


@router.get("/tasks/{task_id}/agents")
async def task_agents(task_id: str) -> dict:
    if not rows("SELECT id FROM agent_tasks WHERE id=?", (task_id,)):
        raise HTTPException(404, "任务不存在")
    return task_agent_trace(task_id)
