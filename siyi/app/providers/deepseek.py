from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

from app.config import settings
from app.providers.base import LLMProvider, ProviderCapabilities
from app.providers.configuration import ProviderConfiguration, validate_provider_configuration
from app.providers.descriptors import descriptor_for_configuration
from app.providers.provider import completion as transport_completion
from app.providers.provider import provider_health as transport_health
from app.providers.provider import provider_profile as transport_profile
from app.providers.provider import _provider_endpoint


class DeepSeekProvider(LLMProvider):
    id = "deepseek"
    name = "DeepSeek"

    def __init__(self, config: ProviderConfiguration | None = None) -> None:
        self.config = validate_provider_configuration(config or ProviderConfiguration())
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
        requested_max_tokens = int(kwargs.pop("max_tokens", self.config.max_tokens))
        kwargs.setdefault("base_url", self.descriptor.endpoint)
        kwargs.setdefault("model", self.model)
        kwargs.setdefault("timeout_seconds", self.descriptor.timeout)
        kwargs.setdefault("max_retries", self.descriptor.retry_policy.max_retries)
        kwargs.setdefault("credential_policy", self.descriptor.credential_policy)
        kwargs["allow_legacy_environment"] = not self.config.credential_generation
        return await transport_completion(
            messages,
            tools=tools if self.config.allow_tools else None,
            event_callback=event_callback if self.config.allow_streaming else None,
            max_tokens=min(max(1, requested_max_tokens), self.config.max_tokens),
            **kwargs,
        )

    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        return await transport_health(
            api_key,
            base_url=self.descriptor.endpoint,
            model=self.model,
            timeout_seconds=min(self.descriptor.timeout, 30),
            provider_id_override=self.id,
            credential_policy=self.descriptor.credential_policy,
            allow_legacy_environment=not self.config.credential_generation,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        # Keep context machinery lazy so importing the provider registry does
        # not add work to the sidecar readiness-critical startup path.
        from app.context.budget import model_context_profile

        context = model_context_profile(base_url=self.descriptor.endpoint, model=self.model)
        return ProviderCapabilities(
            streaming=self.config.allow_streaming,
            native_tool_calls=self.config.allow_tools,
            structured_output=True,
            vision=False,
            reasoning=True,
            json_mode=True,
            embeddings=False,
            context_window=context.context_window_tokens,
            default_max_output_tokens=context.max_output_tokens,
            source=context.capability_source,
        )

    def profile(self) -> dict[str, Any]:
        # Keep the persisted DeepSeek profile byte-for-byte compatible with
        # pre-v9 tasks so interrupted work can resume safely.
        profile = transport_profile()
        return {
            **profile,
            "id": self.id,
            "name": self.name,
            "request_url": self.descriptor.endpoint,
            "chat_endpoint": _provider_endpoint(self.descriptor.endpoint, "chat/completions"),
            "default_model": self.model,
            "models": sorted({*profile.get("models", []), self.model}),
            "credential_policy": self.descriptor.credential_policy,
            "descriptor": asdict(self.descriptor),
            "local": False,
        }
