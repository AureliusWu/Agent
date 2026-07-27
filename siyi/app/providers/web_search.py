from __future__ import annotations

import html
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.security.network_security import NetworkPolicyError, guarded_request
from app.security.trust import secure_untrusted_payload


@dataclass(frozen=True)
class SearchRequest:
    query: str
    max_results: int = 8
    topic: str = "general"
    time_range: str | None = None


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: str | None
    relevance: float | None
    provider: str


@dataclass(frozen=True)
class SearchResponse:
    provider: str
    request_id: str
    query: str
    results: tuple[SearchResult, ...]
    duration_ms: int


class SearchProvider(Protocol):
    name: str

    async def health(self) -> dict[str, Any]: ...
    async def search(self, request: SearchRequest) -> SearchResponse: ...


class _HttpSearchProvider:
    name = "unknown"
    endpoint = ""
    allowed_domains: tuple[str, ...] = ()

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key.strip()

    async def health(self) -> dict[str, Any]:
        if not self.api_key:
            return {"provider": self.name, "status": "unconfigured", "latency_ms": None}
        started = time.perf_counter()
        try:
            await self.search(SearchRequest("OpenAI", max_results=1))
            return {"provider": self.name, "status": "ok", "latency_ms": round((time.perf_counter() - started) * 1000)}
        except Exception as exc:
            return {"provider": self.name, "status": "error", "latency_ms": round((time.perf_counter() - started) * 1000), "error": str(exc)}


class TavilySearchProvider(_HttpSearchProvider):
    name = "tavily"
    endpoint = "https://api.tavily.com/search"
    allowed_domains = ("api.tavily.com",)

    async def search(self, request: SearchRequest) -> SearchResponse:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "query": request.query,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "max_results": min(max(request.max_results, 1), 20),
            "topic": request.topic if request.topic in {"general", "news", "finance"} else "general",
        }
        if request.time_range:
            payload["time_range"] = request.time_range
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await guarded_request(
                client, "POST", self.endpoint, purpose="web_search:tavily",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload, allowed_domains=self.allowed_domains, max_response_bytes=2_000_000,
            )
        response.raise_for_status()
        body = response.json()
        results = tuple(
            SearchResult(
                title=str(item.get("title") or "Untitled"), url=str(item.get("url") or ""),
                snippet=str(item.get("content") or ""), published_at=item.get("published_date"),
                relevance=float(item["score"]) if item.get("score") is not None else None, provider=self.name,
            )
            for item in body.get("results") or [] if _public_http_url(str(item.get("url") or ""))
        )
        return SearchResponse(self.name, str(body.get("request_id") or uuid.uuid4().hex), request.query, results, round((time.perf_counter() - started) * 1000))


class BraveSearchProvider(_HttpSearchProvider):
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"
    allowed_domains = ("api.search.brave.com",)

    async def search(self, request: SearchRequest) -> SearchResponse:
        started = time.perf_counter()
        params = httpx.QueryParams({"q": request.query, "count": min(max(request.max_results, 1), 20), "safesearch": "moderate", "extra_snippets": "true"})
        if request.time_range:
            params = params.merge({"freshness": {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(request.time_range, request.time_range)})
        url = f"{self.endpoint}?{params}"
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await guarded_request(
                client, "GET", url, purpose="web_search:brave",
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
                allowed_domains=self.allowed_domains, max_response_bytes=2_000_000,
            )
        response.raise_for_status()
        body = response.json()
        results = tuple(
            SearchResult(
                title=str(item.get("title") or "Untitled"), url=str(item.get("url") or ""),
                snippet="\n".join([str(item.get("description") or ""), *(str(value) for value in item.get("extra_snippets") or [])]).strip(),
                published_at=item.get("page_age"), relevance=None, provider=self.name,
            )
            for item in ((body.get("web") or {}).get("results") or []) if _public_http_url(str(item.get("url") or ""))
        )
        return SearchResponse(self.name, str(response.headers.get("x-request-id") or uuid.uuid4().hex), request.query, results, round((time.perf_counter() - started) * 1000))


def _public_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def provider_for(name: str, credentials: dict[str, str] | None = None) -> SearchProvider:
    values = credentials or {}
    normalized = name.strip().lower()
    if normalized == "brave":
        return BraveSearchProvider(values.get("brave") or settings.brave_api_key)
    if normalized == "tavily":
        return TavilySearchProvider(values.get("tavily") or settings.tavily_api_key)
    raise ValueError(f"Unsupported search provider: {name}")


async def search_web(query: str, *, provider: str = "tavily", credentials: dict[str, str] | None = None, max_results: int = 8, topic: str = "general", time_range: str | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        implementation = provider_for(provider, credentials)
        if not getattr(implementation, "api_key", ""):
            return {"success": False, "status": "unavailable", "error_code": "search_provider_unconfigured", "error_message": f"Search provider {provider} is not configured"}
        response = await implementation.search(SearchRequest(query=query, max_results=max_results, topic=topic, time_range=time_range))
        payload = asdict(response)
        secured, sensitive, findings = secure_untrusted_payload(payload, f"web_search:{provider}")
        return {
            "success": True, "status": "ok", "data": secured, "provider": response.provider,
            "request_id": response.request_id, "query": query, "results": secured.get("results", []),
            "result_count": len(response.results), "duration_ms": response.duration_ms,
            "security": {"redactions": sensitive.redactions, "findings": findings},
        }
    except (httpx.HTTPError, NetworkPolicyError, ValueError) as exc:
        return {"success": False, "status": "error", "error_code": "search_failed", "error_message": str(exc), "retryable": isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)), "duration_ms": round((time.perf_counter() - started) * 1000)}


async def compare_search_providers(query: str, credentials: dict[str, str]) -> dict[str, Any]:
    first = await search_web(query, provider="tavily", credentials=credentials)
    second = await search_web(query, provider="brave", credentials=credentials)
    first_domains = {urlparse(str(item.get("url") or "")).hostname for item in first.get("results") or []}
    second_domains = {urlparse(str(item.get("url") or "")).hostname for item in second.get("results") or []}
    return {
        "query": query, "providers": {"tavily": first, "brave": second},
        "comparison": {"shared_domains": sorted(str(item) for item in first_domains & second_domains if item), "tavily_unique_domains": len(first_domains - second_domains), "brave_unique_domains": len(second_domains - first_domains)},
    }


async def fetch_web_page(url: str, *, max_chars: int = 40_000) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await guarded_request(client, "GET", url, purpose="web_fetch", max_response_bytes=2_000_000)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"text/html", "text/plain", "application/json", "application/xml", "text/xml"}:
            raise NetworkPolicyError(f"Unsupported web content type: {content_type or 'unknown'}")
        text = response.text
        if content_type == "text/html":
            text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = html.unescape(re.sub(r"[ \t\r\f\v]+", " ", text))
            text = re.sub(r"\n\s*\n+", "\n", text).strip()
        truncated = len(text) > max_chars
        secured, sensitive, findings = secure_untrusted_payload({"url": str(response.url), "content_type": content_type, "content": text[:max_chars]}, "web_fetch")
        return {"success": True, "status": "ok", "data": secured, **secured, "truncated": truncated, "security": {"redactions": sensitive.redactions, "findings": findings}}
    except (httpx.HTTPError, NetworkPolicyError) as exc:
        return {"success": False, "status": "error", "error_code": "web_fetch_failed", "error_message": str(exc), "retryable": isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))}
