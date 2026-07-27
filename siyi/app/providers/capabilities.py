from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from app.config import settings
from app.database import connect, now_iso, rows


CapabilityState = Literal["supported", "unsupported", "unknown"]
CAPABILITY_KEYS = ("streaming", "native_tool_calls", "vision", "audio", "reasoning_effort")


@dataclass(frozen=True)
class ProviderCapabilityMatrix:
    provider: str
    model: str
    status: str
    capabilities: dict[str, CapabilityState]
    client_capabilities: dict[str, bool]
    latency_ms: int | None
    sample_count: int
    success_count: int
    observed_at: str | None
    stale: bool
    source: str


def provider_identity(base_url: str) -> tuple[str, str]:
    normalized = base_url.rstrip("/").casefold()
    provider = urlparse(normalized).netloc or "openai-compatible"
    return provider, hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


def _capabilities(value: str | dict[str, Any] | None) -> dict[str, CapabilityState]:
    if isinstance(value, dict):
        payload = value
    else:
        try:
            payload = json.loads(value or "{}")
        except (TypeError, ValueError):
            payload = {}
    return {
        key: state if (state := str(payload.get(key) or "unknown")) in {"supported", "unsupported", "unknown"} else "unknown"
        for key in CAPABILITY_KEYS
    }


def record_provider_observation(
    *,
    base_url: str,
    model: str,
    status: str,
    latency_ms: int | None = None,
    streaming: bool | None = None,
    native_tool_calls: bool | None = None,
    vision: bool | None = None,
    audio: bool | None = None,
    reasoning_effort: bool | None = None,
    error: str | None = None,
    ttl_seconds: int = 86_400,
) -> dict[str, Any]:
    provider, endpoint_hash = provider_identity(base_url)
    existing = rows(
        "SELECT * FROM provider_capabilities WHERE provider=? AND endpoint_hash=? AND model=?",
        (provider, endpoint_hash, model),
    )
    capabilities = _capabilities(existing[0].get("capabilities") if existing else None)
    for key, observed in {
        "streaming": streaming,
        "native_tool_calls": native_tool_calls,
        "vision": vision,
        "audio": audio,
        "reasoning_effort": reasoning_effort,
    }.items():
        if observed is not None:
            capabilities[key] = "supported" if observed else "unsupported"
    previous_samples = int(existing[0].get("sample_count") or 0) if existing else 0
    previous_successes = int(existing[0].get("success_count") or 0) if existing else 0
    sample_count = previous_samples + 1
    success_count = previous_successes + int(status == "ok")
    observed_at = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO provider_capabilities(provider,endpoint_hash,model,status,capabilities,latency_ms,sample_count,success_count,last_error,observed_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,endpoint_hash,model) DO UPDATE SET "
            "status=excluded.status,capabilities=excluded.capabilities,latency_ms=excluded.latency_ms,sample_count=excluded.sample_count,"
            "success_count=excluded.success_count,last_error=excluded.last_error,observed_at=excluded.observed_at,expires_at=excluded.expires_at",
            (
                provider,
                endpoint_hash,
                model,
                status,
                json.dumps(capabilities, ensure_ascii=False, sort_keys=True),
                latency_ms,
                sample_count,
                success_count,
                (error or "")[:1000] or None,
                observed_at,
                time.time() + max(60, ttl_seconds),
            ),
        )
    return provider_capability_matrix(base_url=base_url, model=model)


def provider_capability_matrix(*, base_url: str | None = None, model: str | None = None) -> dict[str, Any]:
    resolved_url = base_url or settings.model_base_url
    resolved_model = model or settings.model_name
    provider, endpoint_hash = provider_identity(resolved_url)
    records = rows(
        "SELECT * FROM provider_capabilities WHERE provider=? AND endpoint_hash=? AND model=?",
        (provider, endpoint_hash, resolved_model),
    )
    record = records[0] if records else {}
    matrix = ProviderCapabilityMatrix(
        provider=provider,
        model=resolved_model,
        status=str(record.get("status") or "unknown"),
        capabilities=_capabilities(record.get("capabilities")),
        client_capabilities={
            "streaming": True,
            "native_tool_calls": True,
            "vision": False,
            "audio": False,
            "reasoning_effort": False,
        },
        latency_ms=int(record["latency_ms"]) if record.get("latency_ms") is not None else None,
        sample_count=int(record.get("sample_count") or 0),
        success_count=int(record.get("success_count") or 0),
        observed_at=record.get("observed_at"),
        stale=not record or float(record.get("expires_at") or 0) < time.time(),
        source="observed" if record else "client_contract",
    )
    return asdict(matrix)


def configured_provider_matrix() -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for model in settings.model_routes.values():
        if model in seen:
            continue
        seen.add(model)
        result.append(provider_capability_matrix(model=model))
    return result
