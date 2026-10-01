from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from app.config import settings
from app.database import connect, now_iso, rows


CapabilityState = Literal["supported", "unsupported", "unknown"]
CAPABILITY_KEYS = (
    "streaming",
    "native_tool_calls",
    "vision",
    "audio",
    "reasoning_effort",
    "json_mode",
    "embeddings",
)


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
    context_observation: dict[str, Any] | None


def provider_identity(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url.strip())
    host = (parsed.hostname or "").casefold()
    host = f"[{host}]" if ":" in host else host
    port = parsed.port
    scheme = parsed.scheme.casefold()
    authority = host + (f":{port}" if port is not None and (scheme, port) not in {("https", 443), ("http", 80)} else "")
    # URL paths are case-sensitive; userinfo, query and fragment are never
    # provider identity material or persisted diagnostics.
    normalized = urlunsplit((scheme, authority, parsed.path.rstrip("/"), "", ""))
    provider = authority or "openai-compatible"
    return provider, hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


def _payload(value: str | dict[str, Any] | None) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}") if not isinstance(value, dict) else value
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _context_observation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    # Metadata has an explicit no-secret allowlist, independent of arbitrary
    # provider response fields.
    result = {key: value.get(key) for key in (
        "version", "provider_id", "configuration_hash", "model_digest", "theoretical", "configured", "runtime", "observed_at", "expires_at",
    )}
    if result["provider_id"] not in {"deepseek", "ollama", "openai_compatible", "mock"}:
        return None
    for key in ("configuration_hash", "model_digest"):
        item = result[key]
        if item is None and key == "model_digest":
            continue
        if not isinstance(item, str) or len(item) != 64 or any(char not in "0123456789abcdef" for char in item):
            return None
    for key in ("theoretical", "configured", "runtime"):
        if result[key] is not None and (type(result[key]) is not int or not 0 < result[key] <= 10_000_000):
            return None
    if not isinstance(result["observed_at"], str) or len(result["observed_at"]) > 40:
        return None
    if type(result["expires_at"]) not in (int, float) or not 0 <= result["expires_at"] < 100_000_000_000:
        return None
    return result


def _capabilities(value: str | dict[str, Any] | None) -> dict[str, CapabilityState]:
    payload = _payload(value)
    return {
        key: state if (state := str(payload.get(key) or "unknown")) in {"supported", "unsupported", "unknown"} else "unknown"
        for key in CAPABILITY_KEYS
    }


def observation_is_fresh(expires_at: Any) -> bool:
    # Strict numeric bounds reject bool, strings, NaN, +/-inf and huge integers
    # without attempting a lossy float conversion of persisted data.
    return type(expires_at) in (int, float) and time.time() < expires_at < 100_000_000_000


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
    json_mode: bool | None = None,
    embeddings: bool | None = None,
    error: str | None = None,
    ttl_seconds: int = 86_400,
    context_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider, endpoint_hash = provider_identity(base_url)
    existing = rows(
        "SELECT * FROM provider_capabilities WHERE provider=? AND endpoint_hash=? AND model=?",
        (provider, endpoint_hash, model),
    )
    previous = _payload(existing[0].get("capabilities") if existing else None)
    capabilities: dict[str, Any] = _capabilities(previous)
    context = _context_observation(context_observation if context_observation is not None else previous.get("_context_v1"))
    old_context = _context_observation(previous.get("_context_v1")) or {}
    identity_changed = context_observation is not None and context is not None and any(
        context.get(key) != old_context.get(key) for key in ("provider_id", "configuration_hash", "model_digest")
    )
    if identity_changed:
        capabilities = _capabilities(None)
    if context is not None:
        capabilities["_context_v1"] = context
    for key, observed in {
        "streaming": streaming,
        "native_tool_calls": native_tool_calls,
        "vision": vision,
        "audio": audio,
        "reasoning_effort": reasoning_effort,
        "json_mode": json_mode,
        "embeddings": embeddings,
    }.items():
        if observed is not None:
            capabilities[key] = "supported" if observed else "unsupported"
    previous_samples = int(existing[0].get("sample_count") or 0) if existing and not identity_changed else 0
    previous_successes = int(existing[0].get("success_count") or 0) if existing and not identity_changed else 0
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
    from app.providers.configuration import load_provider_configuration
    from app.providers.descriptors import descriptor_for_configuration

    descriptor = descriptor_for_configuration(load_provider_configuration())
    resolved_url = base_url or descriptor.endpoint
    resolved_model = model or descriptor.model
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
            "json_mode": True,
            "embeddings": False,
        },
        latency_ms=int(record["latency_ms"]) if record.get("latency_ms") is not None else None,
        sample_count=int(record.get("sample_count") or 0),
        success_count=int(record.get("success_count") or 0),
        observed_at=record.get("observed_at"),
        stale=not record or not observation_is_fresh(record.get("expires_at")),
        source="observed" if record else "client_contract",
        context_observation=_context_observation(_payload(record.get("capabilities")).get("_context_v1")),
    )
    return asdict(matrix)


def configured_provider_matrix() -> list[dict[str, Any]]:
    from app.providers.configuration import load_provider_configuration
    from app.providers.descriptors import descriptor_for_configuration

    configuration = load_provider_configuration()
    descriptor = descriptor_for_configuration(configuration)
    if configuration.provider_id != "deepseek":
        return [
            provider_capability_matrix(
                base_url=descriptor.endpoint,
                model=descriptor.model,
            )
        ]
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for model in settings.model_routes.values():
        if model in seen:
            continue
        seen.add(model)
        result.append(provider_capability_matrix(model=model))
    return result
