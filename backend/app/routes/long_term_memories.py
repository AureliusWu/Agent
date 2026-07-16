from fastapi import APIRouter, HTTPException, Query

from ..database import rows
from ..long_term_memory import (
    create_memory,
    decide_candidate,
    delete_memory,
    get_memory,
    list_memories,
    memory_history,
    retrieve_memories,
    submit_candidate,
    update_memory,
)
from ..schemas import LongTermMemoryCreate, LongTermMemoryUpdate, MemoryCandidateCreate, MemoryCandidateDecision
from ..memory_consolidator import consolidate_memories, latest_continuity


router = APIRouter(prefix="/api/long-term-memories", tags=["long-term-memories"])


@router.get("")
def memories(memory_type: str | None = None, status: str = "active", include_sensitive: bool = True) -> list[dict]:
    try:
        return list_memories(memory_type=memory_type, status=status, include_sensitive=include_sensitive)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("")
def add_memory(payload: LongTermMemoryCreate) -> dict:
    try:
        return create_memory(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/retrieve")
def retrieve(q: str = Query(min_length=1), limit: int = Query(default=8, ge=1, le=20)) -> list[dict]:
    return retrieve_memories(q, limit=limit)


@router.get("/candidates")
def candidates(status: str = "pending") -> list[dict]:
    return rows("SELECT * FROM memory_candidates WHERE status=? ORDER BY created_at DESC", (status,))


@router.post("/consolidate")
def consolidate() -> dict:
    return consolidate_memories(trigger_type="manual")


@router.get("/continuity")
def continuity() -> dict:
    return latest_continuity()


@router.post("/candidates")
def add_candidate(payload: MemoryCandidateCreate) -> dict:
    try:
        return submit_candidate(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/candidates/{candidate_id}/decision")
def candidate_decision(candidate_id: str, payload: MemoryCandidateDecision) -> dict:
    try:
        return decide_candidate(candidate_id, **payload.model_dump())
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{memory_id}")
def memory(memory_id: str) -> dict:
    try:
        return get_memory(memory_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{memory_id}/history")
def history(memory_id: str) -> list[dict]:
    try:
        return memory_history(memory_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.patch("/{memory_id}")
def edit_memory(memory_id: str, payload: LongTermMemoryUpdate) -> dict:
    changes = payload.model_dump(exclude_unset=True)
    confirmed = bool(changes.pop("administrator_confirmed", False))
    try:
        return update_memory(memory_id, changes, administrator_confirmed=confirmed)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{memory_id}")
def remove_memory(memory_id: str, administrator_confirmed: bool = False) -> dict:
    try:
        return delete_memory(memory_id, administrator_confirmed=administrator_confirmed)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
