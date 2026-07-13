from fastapi import APIRouter, Header, HTTPException, Query

from ..database import rows
from ..multi_agent import task_agent_trace
from ..recovery import list_checkpoints
from ..schemas import ChatRequest, TaskResumeRequest
from ..task_runner import cancel_task, pause_task, run_chat
from ..task_state import RESUMABLE_TASK_STATUSES

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat")
async def chat(payload: ChatRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    return await run_chat(payload, x_model_api_key)


@router.post("/tasks/{task_id}/cancel")
async def stop_task(task_id: str) -> dict:
    return cancel_task(task_id)


@router.post("/tasks/{task_id}/pause")
async def pause_running_task(task_id: str) -> dict:
    return await pause_task(task_id)


@router.post("/tasks/{task_id}/abandon")
async def abandon_task(task_id: str) -> dict:
    return cancel_task(task_id)


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, payload: TaskResumeRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    tasks = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not tasks:
        raise HTTPException(404, "任务不存在")
    task = tasks[0]
    return await run_chat(
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
