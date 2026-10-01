"""One read-only, credential-free capability snapshot for budget/UI/benchmarks.

Model declarations, explicit endpoint-scoped overrides and observations remain
separate. A theoretical model window is never evidence of an active local
runtime window. Reading this module never probes or downloads a model.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from app.config import settings
from app.database import now_iso
from app.providers.capabilities import CAPABILITY_KEYS, observation_is_fresh, provider_capability_matrix, provider_identity, record_provider_observation
from app.providers.configuration import ProviderConfiguration, load_provider_configuration
from app.providers.descriptors import descriptor_for_configuration


DEEPSEEK_V4_PROFILE = {
    "context_window_tokens": 1_000_000, "max_output_tokens": 384_000,
    "supports_tools": True, "supports_parallel_tools": True,
    "supports_json_schema": True, "supports_streaming": True,
    "supports_usage_reporting": True, "capability_source": "provider_registry",
}


def _positive(value: Any) -> int | None:
    return value if type(value) is int and 0 < value <= 10_000_000 else None


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def context_profile_key(configuration: ProviderConfiguration, *, model: str | None = None, base_url: str | None = None) -> str:
    descriptor = descriptor_for_configuration(configuration)
    return f"{configuration.provider_id}:{provider_identity(base_url or descriptor.endpoint)[1]}:{model or descriptor.model}"


def configuration_identity(configuration: ProviderConfiguration) -> str:
    descriptor = descriptor_for_configuration(configuration)
    payload = asdict(configuration)
    payload["base_url"] = provider_identity(descriptor.endpoint)[1]
    payload["model"] = descriptor.model
    return _hash(payload)


def record_context_observation(
    configuration: ProviderConfiguration, *, theoretical: int | None = None,
    configured: int | None = None, runtime: int | None = None,
    model_digest: str | None = None, ttl_seconds: int = 300,
) -> None:
    descriptor = descriptor_for_configuration(configuration)
    digest = (model_digest or "").removeprefix("sha256:").casefold()
    digest = digest if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest) else None
    record_provider_observation(
        base_url=descriptor.endpoint, model=descriptor.model, status="ok",
        context_observation={
            "version": 1, "provider_id": configuration.provider_id,
            "configuration_hash": configuration_identity(configuration), "model_digest": digest,
            "theoretical": _positive(theoretical), "configured": _positive(configured), "runtime": _positive(runtime),
            "observed_at": now_iso(), "expires_at": time.time() + min(300, max(1, ttl_seconds)),
        },
    )


@dataclass(frozen=True)
class EffectiveCapabilities:
    provider_id: str
    endpoint_hash: str
    model: str
    configuration_hash: str
    identity_hash: str
    model_digest: str | None
    context_window_tokens: int | None
    theoretical_context_window: int | None
    configured_context_window: int | None
    runtime_context_window: int | None
    max_output_tokens: int
    window_status: str
    capability_source: str
    declared_capabilities: dict[str, Any]
    observed_capabilities: dict[str, str]
    explicit_profile: dict[str, Any]
    observation_stale: bool
    observed_at: str | None
    local: bool
    file_agent_qualified: bool = False
    qualification: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "context_profile_key": f"{self.provider_id}:{self.endpoint_hash}:{self.model}"}


def resolve_effective_capabilities(
    *, configuration: ProviderConfiguration | None = None,
    base_url: str | None = None, model: str | None = None,
) -> EffectiveCapabilities:
    config = configuration or load_provider_configuration()
    original = descriptor_for_configuration(config)
    endpoint = base_url or original.endpoint
    resolved_model = model or original.model
    # Explicit URL consumers (e.g. a remote diagnostic) must not accidentally
    # inherit the currently selected local provider's identity.
    if provider_identity(endpoint) != provider_identity(original.endpoint):
        selected = "deepseek" if (urlsplit(endpoint).hostname or "").casefold() == "api.deepseek.com" else "openai_compatible"
        config = replace(config, provider_id=selected, base_url=endpoint, model=resolved_model)
    else:
        config = replace(config, model=resolved_model) if config.provider_id != "mock" else config
    descriptor = descriptor_for_configuration(config)
    endpoint_hash = provider_identity(endpoint)[1]
    config_hash = configuration_identity(config)
    scoped = settings.model_context_profiles.get(context_profile_key(config, model=resolved_model, base_url=endpoint))
    # Legacy name-only profiles are retained ONLY for official DeepSeek;
    # local/compatible endpoints require a scoped key to prevent reuse after
    # switching a same-named model to a different service.
    official = descriptor.provider_id == "deepseek" and (urlsplit(endpoint).hostname or "").casefold() == "api.deepseek.com"
    legacy = (settings.model_context_profiles.get(resolved_model) or settings.model_context_profiles.get("*")) if official else None
    raw = scoped if isinstance(scoped, dict) else (legacy if isinstance(legacy, dict) else {})
    allowed = {"context_window_tokens", "max_output_tokens", "supports_tools", "supports_parallel_tools", "supports_json_schema", "supports_streaming", "supports_usage_reporting"}
    explicit = {key: value for key, value in raw.items() if key in allowed and (type(value) is bool if key.startswith("supports_") else _positive(value) is not None)}
    matrix = provider_capability_matrix(base_url=endpoint, model=resolved_model)
    raw_observation = matrix.get("context_observation")
    observation = raw_observation if isinstance(raw_observation, dict) else {}
    valid = (
        observation.get("provider_id") == config.provider_id
        and observation.get("configuration_hash") == config_hash
        and observation_is_fresh(observation.get("expires_at"))
    )
    active = observation if valid else {}
    theoretical = _positive(active.get("theoretical"))
    configured = _positive(active.get("configured"))
    runtime = _positive(active.get("runtime"))
    declared = asdict(descriptor.capabilities)
    candidates = [number for number in (_positive(explicit.get("context_window_tokens")), configured, runtime) if number is not None]
    source = "explicit_configuration" if explicit.get("context_window_tokens") is not None else "observed_runtime"
    status = "configured" if explicit.get("context_window_tokens") is not None or configured is not None else "observed"
    if candidates:
        window = min([*candidates, *([theoretical] if theoretical is not None else [])])
    elif official and resolved_model in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        window, source, status = DEEPSEEK_V4_PROFILE["context_window_tokens"], "provider_registry", "declared"
    elif config.provider_id == "mock":
        window, source, status = 65_536, "deterministic_contract", "declared"
    else:
        window, source, status = None, "unknown", "unknown"
    cap = min(config.max_tokens, _positive(explicit.get("max_output_tokens")) or config.max_tokens)
    if window is not None:
        cap = min(cap, max(0, window // 2))
    observed = matrix["capabilities"] if not matrix["stale"] and (not descriptor.local or valid) else {key: "unknown" for key in CAPABILITY_KEYS}
    digest = active.get("model_digest")
    identity = _hash({"configuration_hash": config_hash, "endpoint_hash": endpoint_hash, "model": resolved_model, "digest": digest, "explicit": explicit, "theoretical": theoretical, "configured": configured, "runtime": runtime})
    snapshot = EffectiveCapabilities(
        config.provider_id, endpoint_hash, resolved_model, config_hash, identity, digest,
        window, theoretical, configured, runtime, cap, status, source,
        declared, observed, explicit, not valid, active.get("observed_at"), descriptor.local,
    )
    from app.providers.qualification_store import qualification_status
    qualification = qualification_status(asdict(snapshot))
    return replace(snapshot, qualification=qualification,
                   file_agent_qualified=qualification["levels"]["file_agent"]["qualified"])
