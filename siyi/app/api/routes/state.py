from fastapi import APIRouter, HTTPException

from app.personality.affect import current_affect, current_relationship, record_affect_event, update_relationship
from app.schemas import AffectEventCreate, RelationshipEventCreate


router = APIRouter(prefix="/api/state", tags=["state"])


@router.get("")
def state() -> dict:
    return {"affect": current_affect(), "relationship": current_relationship()}


@router.post("/affect-events")
def add_affect_event(payload: AffectEventCreate) -> dict:
    return record_affect_event(**payload.model_dump())


@router.post("/relationship-events")
def add_relationship_event(payload: RelationshipEventCreate) -> dict:
    try:
        return update_relationship(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
