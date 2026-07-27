from fastapi import APIRouter, HTTPException

from app.personality.identity_service import active_identity, activate_identity_version, create_identity_version, identity_versions
from app.schemas import IdentityVersionActivate, IdentityVersionCreate


router = APIRouter(prefix="/api/identity", tags=["identity"])


@router.get("")
def get_identity() -> dict:
    return active_identity()


@router.get("/versions")
def get_identity_versions() -> list[dict]:
    return identity_versions()


@router.post("/versions")
def add_identity_version(payload: IdentityVersionCreate) -> dict:
    try:
        return create_identity_version(
            payload.identity,
            reason=payload.reason,
            actor_id=payload.actor_id,
            administrator_confirmed=payload.administrator_confirmed,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/activate")
def activate_version(payload: IdentityVersionActivate) -> dict:
    try:
        return activate_identity_version(
            payload.version,
            actor_id=payload.actor_id,
            administrator_confirmed=payload.administrator_confirmed,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
