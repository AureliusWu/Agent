from fastapi import APIRouter, HTTPException

from ..database import audit
from ..memory import (
    delete_workspace_memory,
    list_workspace_memories,
    memory_feedback,
    update_workspace_memory,
    upsert_workspace_memory,
)
from ..schemas import MemoryCreate, MemoryFeedback, MemoryUpdate


router = APIRouter(prefix="/api/memories", tags=["memories"])


@router.get("")
def list_memories(workspace: str, kind: str | None = None, include_rejected: bool = True) -> list[dict]:
    try:
        return list_workspace_memories(workspace, kind, include_rejected=include_rejected)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("")
def create_memory(workspace: str, payload: MemoryCreate) -> dict:
    try:
        result = upsert_workspace_memory(workspace, source="user", verified=True, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "create_memory", result["key"], "ok", {"workspace": workspace, "kind": result["kind"]})
    return result


@router.patch("/{memory_id}")
def update_memory(memory_id: int, workspace: str, payload: MemoryUpdate) -> dict:
    try:
        result = update_workspace_memory(workspace, memory_id, payload.model_dump(exclude_unset=True))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "update_memory", str(memory_id), "ok", {"workspace": workspace})
    return result


@router.delete("/{memory_id}")
def delete_memory(memory_id: int, workspace: str) -> dict:
    if not delete_workspace_memory(workspace, memory_id):
        raise HTTPException(404, "记忆不存在")
    audit(None, "delete_memory", str(memory_id), "ok", {"workspace": workspace})
    return {"id": memory_id, "deleted": True}


@router.post("/{memory_id}/feedback")
def feedback(memory_id: int, workspace: str, payload: MemoryFeedback) -> dict:
    try:
        result = memory_feedback(workspace, memory_id, payload.outcome)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "memory_feedback", str(memory_id), "ok", {"workspace": workspace, "outcome": payload.outcome})
    return result
