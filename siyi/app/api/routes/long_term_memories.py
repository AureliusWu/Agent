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
    LongTermMemorySearchRequest,
    LongTermMemoryUpdate,
    MemoryCandidateCreate,
    MemoryCandidateDecision,
)
from app.memory.consolidation import consolidate_memories, latest_continuity
from app.memory.search import MemorySearchQuery, memory_search_service


router = APIRouter(prefix="/api/long-term-memories", tags=["long-term-memories"])


@router.get("")
def memories(memory_type: str | None = None, status: str = "active", include_sensitive: bool = False) -> list[dict]:
    if include_sensitive:
        raise HTTPException(403, "敏感记忆只能通过管理员授权搜索接口读取")
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


@router.post("/search")
def search_memories(payload: LongTermMemorySearchRequest) -> dict:
    values = payload.model_dump()
    token = values.pop("admin_grant_token")
    ui_session_id = values.pop("ui_session_id")
    grant_payload = payload.model_dump(exclude_unset=True, exclude={"admin_grant_token", "ui_session_id"})
    try:
        if payload.sensitive_mode != "exclude":
            consume_admin_action_grant(
                token or "",
                operation="memory.search_sensitive",
                target_id="search",
                payload=grant_payload,
                ui_session_id=ui_session_id or "",
            )
        return memory_search_service.search(
            MemorySearchQuery(
                query=payload.query,
                memory_types=tuple(payload.memory_types),
                statuses=tuple(payload.statuses),
                source_types=tuple(payload.source_types),
                user_confirmed=payload.user_confirmed,
                is_locked=payload.is_locked,
                valid_from=payload.valid_from,
                valid_to=payload.valid_to,
                min_importance=payload.min_importance,
                min_confidence=payload.min_confidence,
                sensitive_mode=payload.sensitive_mode,
                sort=payload.sort,
                offset=payload.offset,
                limit=payload.limit,
                cursor=payload.cursor,
            )
        )
    except AdminActionGrantError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


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
