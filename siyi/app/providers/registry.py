from __future__ import annotations

import os
from typing import Any

from app.config import settings
from app.providers.base import FailureCategory, LLMProvider
from app.providers.base import ProviderDescriptor
from app.providers.configuration import ProviderConfiguration, configuration_for_provider, load_provider_configuration
from app.providers.deepseek import DeepSeekProvider
from app.providers.descriptors import descriptor_for_configuration
from app.providers.mock import MockProvider
from app.providers.ollama import OllamaProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
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
        "unsupported_capability",
        "context_window_unknown",
        "context_window_exceeded",
        "cost_pricing_unknown",
        "cost_usage_unknown",
        "cost_lease_required",
        "cost_input_unknown",
    }:
        return FailureCategory.ENVIRONMENT_FAILURE
    if error_type in {"timeout", "network_error", "cancelled", "retry_exhausted", "cost_budget_limit"}:
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
        return DeepSeekProvider(config)
    if selected == "ollama":
        return OllamaProvider(config)
    if selected == "openai_compatible":
        return OpenAICompatibleProvider(config)
    if selected == "mock":
        scenario = os.getenv("SIYI_MOCK_SCENARIO", "normal").strip() or "normal"
        return MockProvider(scenario)
    raise ValueError(f"unknown provider: {selected}")


async def completion(messages: list[dict[str, Any]], api_key: str | None = None, **kwargs: Any) -> dict[str, Any]:
    provider = get_provider()
    descriptor = getattr(provider, "descriptor", None)
    if descriptor is not None and descriptor.local and descriptor.provider_id != "mock":
        from app.context.budget import context_window_reason, request_budget

        budget = request_budget(
            messages, kwargs.get("tools"), base_url=descriptor.endpoint, model=descriptor.model,
            configuration=getattr(provider, "config", None),
            desired_output_tokens=int(kwargs.get("max_tokens") or descriptor.capabilities.default_max_output_tokens or settings.model_max_tokens),
        )
        code = budget.blocked_reason or ("context_window_exceeded" if budget.exceeds_context_window or budget.reserved_output_tokens <= 0 else None)
        if code:
            raise ProviderError(context_window_reason(code), code, details={"effective_identity": budget.effective_identity})
        # This boundary also covers planner, verifier and child calls that do
        # not go through the main Runner's model preflight.
        kwargs["max_tokens"] = budget.reserved_output_tokens
        kwargs["context_window_tokens"] = budget.context_window_tokens
        kwargs["reserved_output_tokens"] = budget.reserved_output_tokens
        kwargs["estimated_input_tokens"] = budget.estimated_input_tokens
    if api_key is not None and (descriptor is None or descriptor.credential_policy != "forbidden"):
        kwargs["api_key"] = api_key
    return await provider.chat(messages, **kwargs)


async def provider_health(api_key: str | None = None, provider_id: str | None = None) -> dict[str, Any]:
    return await get_provider(provider_id).health_check(api_key)


def provider_descriptor(
    provider_id: str | None = None,
    configuration: ProviderConfiguration | None = None,
) -> ProviderDescriptor:
    current = configuration or load_provider_configuration()
    selected = provider_id or current.provider_id
    if selected != current.provider_id:
        current = configuration_for_provider(selected, current)
    return descriptor_for_configuration(current)


def provider_profile(provider_id: str | None = None) -> dict[str, Any]:
    from app.providers.effective_capabilities import resolve_effective_capabilities

    provider = get_provider(provider_id)
    configuration = getattr(provider, "config", None) or configuration_for_provider(provider.id, load_provider_configuration())
    return {
        **provider.profile(),
        "effective_capabilities": resolve_effective_capabilities(configuration=configuration).public(),
    }


def provider_ready(api_key: str | None = None) -> bool:
    provider = get_provider()
    descriptor = getattr(provider, "descriptor", None)
    if descriptor is not None and descriptor.credential_policy == "required":
        return bool(api_key or settings.deepseek_api_key)
    return True


def assert_paid_api_allowed(provider_name: str = "deepseek") -> None:
    provider = os.getenv("SIYI_TEST_PROVIDER", "").strip().lower()
    allowed = os.getenv("SIYI_ALLOW_PAID_API", "").strip().lower() == "true"
    expected = provider_name.strip().lower()
    if provider != expected or not allowed:
        raise ProviderError(
            f"付费模型验收被硬门禁阻止；需同时设置 SIYI_TEST_PROVIDER={expected} "
            "和 SIYI_ALLOW_PAID_API=true",
            "paid_api_blocked",
        )
