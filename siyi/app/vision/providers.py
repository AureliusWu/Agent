from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit

from app.config import settings
from app.providers.base import LLMProvider, ProviderCapabilities
from app.providers.provider import ProviderError, completion as transport_completion


class OpenAICompatibleVisionProvider(LLMProvider):
    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        model: str,
        api_key: str | None,
        local: bool,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ProviderError("视觉 Provider URL 无效", "invalid_configuration")
        if local and (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise ProviderError("本地视觉 Provider 必须使用回环地址", "invalid_configuration")
        if not model.strip():
            raise ProviderError("未配置视觉模型", "unsupported_capability")
        if not local and not api_key:
            raise ProviderError("远程视觉 Provider 缺少请求级凭据", "missing_api_key")
        self.id = provider_id
        self.name = provider_id
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.api_key = api_key or "local-vision-only"
        self.local = local

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await transport_completion(
            messages,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            provider_id_override=self.id,
            allow_private_provider=self.local,
            event_callback=event_callback,
            **kwargs,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            native_tool_calls=False,
            structured_output=False,
            vision=True,
            reasoning=None,
            json_mode=None,
            embeddings=False,
            local=self.local,
            context_window=settings.default_model_context_window,
            default_max_output_tokens=min(settings.model_max_tokens, 4096),
            source="explicit_vision_configuration",
        )

    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        del api_key
        return {
            "status": "configured",
            "provider": self.id,
            "model": self.model,
            "local": self.local,
        }

    def profile(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "api_format": "OpenAI-compatible vision",
            "request_url": self.base_url,
            "default_model": self.model,
            "models": [self.model],
            "local": self.local,
            "credential_env": "",
        }


def configured_remote_provider(api_key: str | None) -> OpenAICompatibleVisionProvider:
    if not settings.vision_remote_base_url or not settings.vision_remote_model:
        raise ProviderError("未配置远程视觉 Provider", "unsupported_capability")
    return OpenAICompatibleVisionProvider(
        provider_id="vision-remote",
        base_url=settings.vision_remote_base_url,
        model=settings.vision_remote_model,
        api_key=api_key,
        local=False,
    )


def configured_local_provider() -> OpenAICompatibleVisionProvider:
    if not settings.vision_local_model:
        raise ProviderError("未配置本地视觉模型；司忆不会自动下载模型", "unsupported_capability")
    return OpenAICompatibleVisionProvider(
        provider_id="vision-local",
        base_url=settings.vision_local_base_url,
        model=settings.vision_local_model,
        api_key=None,
        local=True,
    )
