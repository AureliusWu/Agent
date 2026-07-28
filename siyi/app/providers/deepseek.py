from __future__ import annotations

from typing import Any, Callable

from app.config import settings
from app.providers.base import LLMProvider, ProviderCapabilities
from app.providers.provider import completion as transport_completion
from app.providers.provider import provider_health as transport_health
from app.providers.provider import provider_profile as transport_profile


class DeepSeekProvider(LLMProvider):
    id = "deepseek"
    name = "DeepSeek"

    def __init__(self) -> None:
        self.model = settings.model_name

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await transport_completion(
            messages,
            tools=tools,
            event_callback=event_callback,
            **kwargs,
        )

    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        return await transport_health(api_key)

    def get_capabilities(self) -> ProviderCapabilities:
        # Keep context machinery lazy so importing the provider registry does
        # not add work to the sidecar readiness-critical startup path.
        from app.context.budget import model_context_profile

        context = model_context_profile(base_url=settings.model_base_url, model=self.model)
        return ProviderCapabilities(
            streaming=True,
            native_tool_calls=True,
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
        return transport_profile()
