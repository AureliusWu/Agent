from fastapi import APIRouter, HTTPException, Query

from app.admin_action_grants import (
    AdminActionGrantError,
    consume_admin_action_grant,
    issue_admin_action_grant,
)
from app.database import rows
from app.memory.long_term import (
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
from app.schemas import (
    AdminActionGrantCreate,
    LongTermMemoryCreate,
    LongTermMemoryUpdate,
    MemoryCandidateCreate,
    MemoryCandidateDecision,
)
from app.memory.consolidation import consolidate_memories, latest_continuity


router = APIRouter(prefix="/api/long-term-memories", tags=["long-term-memories"])


@router.get("")
def memories(memory_type: str | None = None, status: str = "active", include_sensitive: bool = True) -> list[dict]:
    try:
        return list_memories(memory_type=memory_type, status=status, include_sensitive=include_sensitive)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("")
def add_memory(payload: LongTermMemoryCreate) -> dict:
    values = payload.model_dump()
    token = values.pop("admin_grant_token")
    ui_session_id = values.pop("ui_session_id")
    try:
        consume_admin_action_grant(
            token,
            operation="memory.create",
            target_id="new",
            payload=values,
            ui_session_id=ui_session_id,
        )
        return create_memory(**values)
    except AdminActionGrantError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/admin-action-grants")
def create_admin_action_grant(payload: AdminActionGrantCreate) -> dict:
    try:
        return issue_admin_action_grant(**payload.model_dump())
    except AdminActionGrantError as exc:
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
        authorization = None
        if payload.accept:
            authorization = consume_admin_action_grant(
                payload.admin_grant_token or "",
                operation="memory_candidate.accept",
                target_id=candidate_id,
                payload={"accept": True},
                ui_session_id=payload.ui_session_id or "",
            )
        return decide_candidate(candidate_id, accept=payload.accept, authorization=authorization)
    except AdminActionGrantError as exc:
        raise HTTPException(403, str(exc)) from exc
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
    token = str(changes.pop("admin_grant_token"))
    ui_session_id = str(changes.pop("ui_session_id"))
    try:
        authorization = consume_admin_action_grant(
            token,
            operation="memory.update",
            target_id=memory_id,
            payload=changes,
            ui_session_id=ui_session_id,
        )
        return update_memory(memory_id, changes, authorization=authorization)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{memory_id}")
def remove_memory(memory_id: str, admin_grant_token: str, ui_session_id: str) -> dict:
    try:
        authorization = consume_admin_action_grant(
            admin_grant_token,
            operation="memory.delete",
            target_id=memory_id,
            payload={},
            ui_session_id=ui_session_id,
        )
        return delete_memory(memory_id, authorization=authorization)
    except AdminActionGrantError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
