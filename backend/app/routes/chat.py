from fastapi import APIRouter, Header

from ..schemas import ChatRequest
from ..task_runner import cancel_task, run_chat

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat")
async def chat(payload: ChatRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    return await run_chat(payload, x_model_api_key)


@router.post("/tasks/{task_id}/cancel")
async def stop_task(task_id: str) -> dict:
    return cancel_task(task_id)
