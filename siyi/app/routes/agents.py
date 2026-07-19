from fastapi import APIRouter

from ..agent_profiles import list_agent_profiles


router = APIRouter(prefix="/api", tags=["agents"])


@router.get("/agent-profiles")
def agent_profiles() -> list[dict]:
    return [profile.catalog() for profile in list_agent_profiles()]
