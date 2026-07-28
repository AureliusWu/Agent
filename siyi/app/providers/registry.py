from __future__ import annotations

import os
from typing import Any

from app.config import settings
from app.providers.base import FailureCategory, LLMProvider
from app.providers.configuration import configuration_for_provider, load_provider_configuration
from app.providers.deepseek import DeepSeekProvider
from app.providers.mock import MockProvider
from app.providers.ollama import OllamaProvider
from app.providers.provider import ProviderError


def failure_category(error: BaseException | str) -> FailureCategory:
    error_type = error.error_type if isinstance(error, ProviderError) else str(error)
    if error_type in {"invalid_json", "invalid_response", "empty_response", "response_too_large"}:
        return FailureCategory.PROTOCOL_FAILURE
    if error_type in {"invalid_tool_call", "missing_tool_arguments", "invalid_tool_arguments", "unknown_tool", "duplicate_tool_call"}:
        return FailureCategory.TOOL_FAILURE
    if error_type in {
        "authentication",
        "missing_api_key",
        "network_policy",
        "model_not_found",
        "ollama_service_unavailable",
        "ollama_model_missing",
        "ollama_port_conflict",
        "ollama_invalid_response",
    }:
        return FailureCategory.ENVIRONMENT_FAILURE
    if error_type in {"timeout", "network_error", "cancelled", "retry_exhausted"}:
        return FailureCategory.RUNTIME_FAILURE
    if error_type in {"verification_failed"}:
        return FailureCategory.VERIFICATION_FAILURE
    return FailureCategory.MODEL_FAILURE


def get_provider(provider_id: str | None = None) -> LLMProvider:
    config = load_provider_configuration()
    selected = provider_id or config.provider_id
    if provider_id is not None:
        config = configuration_for_provider(selected, config)
    if selected == "deepseek":
        return DeepSeekProvider()
    if selected == "ollama":
        return OllamaProvider(config)
    if selected == "mock":
        scenario = os.getenv("SIYI_MOCK_SCENARIO", "normal").strip() or "normal"
        return MockProvider(scenario)
    raise ValueError(f"unknown provider: {selected}")


async def completion(messages: list[dict[str, Any]], api_key: str | None = None, **kwargs: Any) -> dict[str, Any]:
    provider = get_provider()
    if api_key is not None and provider.id == "deepseek":
        kwargs["api_key"] = api_key
    return await provider.chat(messages, **kwargs)


async def provider_health(api_key: str | None = None, provider_id: str | None = None) -> dict[str, Any]:
    return await get_provider(provider_id).health_check(api_key)


def provider_profile(provider_id: str | None = None) -> dict[str, Any]:
    return get_provider(provider_id).profile()


def provider_ready(api_key: str | None = None) -> bool:
    provider = get_provider()
    if provider.id == "deepseek":
        return bool(api_key or settings.deepseek_api_key)
    return True


def assert_paid_api_allowed() -> None:
    provider = os.getenv("SIYI_TEST_PROVIDER", "").strip().lower()
    allowed = os.getenv("SIYI_ALLOW_PAID_API", "").strip().lower() == "true"
    if provider != "deepseek" or not allowed:
        raise ProviderError(
            "付费模型验收被硬门禁阻止；需同时设置 SIYI_TEST_PROVIDER=deepseek 和 SIYI_ALLOW_PAID_API=true",
            "paid_api_blocked",
        )
