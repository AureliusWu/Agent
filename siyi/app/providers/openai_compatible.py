from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

from app.providers.base import LLMProvider, ProviderCapabilities
from app.providers.configuration import ProviderConfiguration, validate_provider_configuration
from app.providers.descriptors import descriptor_for_configuration
from app.providers.provider import completion as transport_completion
from app.providers.provider import provider_health as transport_health


class OpenAICompatibleProvider(LLMProvider):
    id = "openai_compatible"
    name = "OpenAI-compatible"

    def __init__(self, config: ProviderConfiguration) -> None:
        self.config = validate_provider_configuration(config)
        self.descriptor = descriptor_for_configuration(self.config)
        self.model = self.descriptor.model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if tools and not self.config.allow_tools:
            self._require_capability("tool_call", False)
        if event_callback is not None and not self.config.allow_streaming:
            self._require_capability("streaming", False)
        supplied_key = kwargs.pop("api_key", None)
        kwargs.pop("base_url", None)
        kwargs.pop("model", None)
        requested_max_tokens = int(kwargs.pop("max_tokens", self.config.max_tokens))
        return await transport_completion(
            messages,
            api_key=None if self.descriptor.credential_policy == "forbidden" else supplied_key,
            tools=tools if self.config.allow_tools else None,
            event_callback=event_callback if self.config.allow_streaming else None,
            base_url=self.descriptor.endpoint,
            model=self.model,
            max_tokens=min(max(1, requested_max_tokens), self.config.max_tokens),
            allow_private_provider=self.descriptor.local,
            provider_id_override=self.id,
            timeout_seconds=self.descriptor.timeout,
            max_retries=self.descriptor.retry_policy.max_retries,
            credential_policy=self.descriptor.credential_policy,
            **kwargs,
        )

    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        return await transport_health(
            None if self.descriptor.credential_policy == "forbidden" else api_key,
            base_url=self.descriptor.endpoint,
            model=self.model,
            timeout_seconds=min(self.descriptor.timeout, 30),
            allow_private_provider=self.descriptor.local,
            provider_id_override=self.id,
            credential_policy=self.descriptor.credential_policy,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        return self.descriptor.capabilities

    def profile(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "official_url": "",
            "docs_url": "",
            "api_format": "OpenAI-compatible",
            "request_url": self.descriptor.endpoint,
            "chat_endpoint": _chat_endpoint(self.descriptor.endpoint),
            "credential_env": "" if self.descriptor.local else "AGENT_DEEPSEEK_API_KEY",
            "credential_policy": self.descriptor.credential_policy,
            "default_model": self.model,
            "models": [self.model],
            "capabilities": self.capabilities(),
            "descriptor": asdict(self.descriptor),
            "local": self.descriptor.local,
        }


def _chat_endpoint(endpoint: str) -> str:
    from app.providers.provider import _provider_endpoint

    return _provider_endpoint(endpoint, "chat/completions")
