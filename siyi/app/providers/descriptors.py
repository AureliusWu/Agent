from __future__ import annotations

from app.config import settings
from app.providers.base import (
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderRetryPolicy,
)
from app.providers.configuration import (
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    ProviderConfiguration,
    endpoint_is_local,
)


def descriptor_for_configuration(config: ProviderConfiguration) -> ProviderDescriptor:
    provider_id = config.provider_id
    timeout = config.timeout_seconds
    retry = ProviderRetryPolicy(max_retries=config.max_retries)

    if provider_id == "deepseek":
        model = config.model.strip() or settings.model_name
        endpoint = (config.base_url.strip() or settings.model_base_url).rstrip("/")
        return ProviderDescriptor(
            provider_id="deepseek",
            display_name="DeepSeek",
            provider_type="cloud",
            endpoint=endpoint,
            model=model,
            credential_policy="required",
            capabilities=ProviderCapabilities(
                streaming=config.allow_streaming,
                native_tool_calls=config.allow_tools,
                structured_output=True,
                local=False,
                vision=False,
                reasoning=True,
                json_mode=True,
                embeddings=False,
                default_max_output_tokens=config.max_tokens,
                source="provider_descriptor_v2",
            ),
            timeout=timeout,
            retry_policy=retry,
            local=False,
            health_strategy="openai_models",
        )

    if provider_id == "ollama":
        return ProviderDescriptor(
            provider_id="ollama",
            display_name="Ollama",
            provider_type="local",
            endpoint=(config.base_url or OLLAMA_BASE_URL).rstrip("/"),
            model=config.model or OLLAMA_MODEL,
            credential_policy="forbidden",
            capabilities=ProviderCapabilities(
                streaming=config.allow_streaming,
                native_tool_calls=config.allow_tools,
                structured_output=True,
                local=True,
                vision=None,
                reasoning=None,
                json_mode=True,
                embeddings=None,
                default_max_output_tokens=config.max_tokens,
                source="provider_descriptor_v2",
            ),
            timeout=timeout,
            retry_policy=retry,
            local=True,
            health_strategy="ollama_diagnostics",
        )

    if provider_id == "openai_compatible":
        local = endpoint_is_local(config.base_url)
        return ProviderDescriptor(
            provider_id="openai_compatible",
            display_name="OpenAI-compatible",
            provider_type="local" if local else "remote",
            endpoint=config.base_url.rstrip("/"),
            model=config.model,
            credential_policy="forbidden" if local else "required",
            capabilities=ProviderCapabilities(
                streaming=config.allow_streaming,
                native_tool_calls=config.allow_tools,
                structured_output=None,
                local=local,
                vision=None,
                reasoning=None,
                json_mode=None,
                embeddings=None,
                default_max_output_tokens=config.max_tokens,
                source="provider_descriptor_v2",
            ),
            timeout=timeout,
            retry_policy=retry,
            local=local,
            health_strategy="openai_models",
        )

    if provider_id == "mock":
        return ProviderDescriptor(
            provider_id="mock",
            display_name="Deterministic Mock",
            provider_type="test",
            endpoint="in-process://mock",
            model="siyi-mock-v1",
            credential_policy="forbidden",
            capabilities=ProviderCapabilities(
                streaming=True,
                native_tool_calls=True,
                structured_output=True,
                local=True,
                vision=False,
                reasoning=False,
                json_mode=True,
                embeddings=False,
                context_window=65_536,
                default_max_output_tokens=8_192,
                source="deterministic_contract",
            ),
            timeout=timeout,
            retry_policy=ProviderRetryPolicy(max_retries=0),
            local=True,
            health_strategy="in_process",
        )

    raise ValueError(f"unknown provider: {provider_id}")
