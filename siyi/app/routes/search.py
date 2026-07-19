from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from ..config import settings
from ..web_search import compare_search_providers, provider_for


router = APIRouter(prefix="/api/search", tags=["search"])


def _credentials(tavily: str | None, brave: str | None) -> dict[str, str]:
    return {"tavily": tavily or settings.tavily_api_key, "brave": brave or settings.brave_api_key}


@router.get("/providers")
async def search_providers(
    x_tavily_api_key: str | None = Header(default=None),
    x_brave_api_key: str | None = Header(default=None),
) -> dict:
    credentials = _credentials(x_tavily_api_key, x_brave_api_key)
    providers = []
    for name in ("tavily", "brave"):
        configured = bool(credentials[name])
        providers.append({"name": name, "configured": configured, "default": name == settings.default_search_provider})
    return {"default": settings.default_search_provider, "providers": providers}


@router.post("/providers/{provider}/health")
async def search_provider_health(
    provider: str,
    x_tavily_api_key: str | None = Header(default=None),
    x_brave_api_key: str | None = Header(default=None),
) -> dict:
    try:
        implementation = provider_for(provider, _credentials(x_tavily_api_key, x_brave_api_key))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return await implementation.health()


@router.post("/compare")
async def compare_search(
    query: str = Query(min_length=1, max_length=2000),
    x_tavily_api_key: str | None = Header(default=None),
    x_brave_api_key: str | None = Header(default=None),
) -> dict:
    credentials = _credentials(x_tavily_api_key, x_brave_api_key)
    if not all(credentials.values()):
        raise HTTPException(409, "Tavily and Brave API keys are both required for comparison")
    return await compare_search_providers(query, credentials)
