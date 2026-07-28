from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Callable

import httpx

from app.providers.base import FailureCategory, LLMProvider, ProviderCapabilities
from app.providers.configuration import OLLAMA_BASE_URL, OLLAMA_MODEL, ProviderConfiguration
from app.providers.provider import completion as transport_completion
from app.security.network_security import guarded_request


class OllamaProvider(LLMProvider):
    id = "ollama"
    name = "Ollama"

    def __init__(self, config: ProviderConfiguration) -> None:
        self.base_url = (config.base_url or OLLAMA_BASE_URL).rstrip("/")
        self.model = config.model or OLLAMA_MODEL
        self.timeout_seconds = config.timeout_seconds
        self.max_tokens = config.max_tokens
        self.allow_tools = config.allow_tools
        self.allow_streaming = config.allow_streaming

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        kwargs.pop("api_key", None)
        kwargs.pop("base_url", None)
        kwargs.pop("model", None)
        return await transport_completion(
            messages,
            api_key="ollama-local-only",
            tools=tools if self.allow_tools else None,
            event_callback=event_callback if self.allow_streaming else None,
            base_url=self.base_url,
            model=self.model,
            max_tokens=min(int(kwargs.pop("max_tokens", self.max_tokens)), self.max_tokens),
            allow_private_provider=True,
            provider_id_override=self.id,
            timeout_seconds=self.timeout_seconds,
            **kwargs,
        )

    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await guarded_request(
                    client,
                    "GET",
                    f"{self.base_url}/api/tags",
                    purpose="local_model_provider_health",
                    allow_private=True,
                )
                response.raise_for_status()
                models = {
                    str(item.get("name") or item.get("model") or "")
                    for item in response.json().get("models", [])
                    if isinstance(item, dict)
                }
            if self.model not in models:
                return {
                    "status": "error",
                    "provider": self.id,
                    "model": self.model,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "error": f"本地模型未安装：{self.model}",
                    "failure_category": FailureCategory.ENVIRONMENT_FAILURE.value,
                }
            return {
                "status": "ok",
                "provider": self.id,
                "model": self.model,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "capabilities": asdict(self.get_capabilities()),
            }
        except Exception as exc:
            return {
                "status": "error",
                "provider": self.id,
                "model": self.model,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": str(exc),
                "failure_category": FailureCategory.ENVIRONMENT_FAILURE.value,
            }

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            native_tool_calls=True,
            structured_output=True,
            local=True,
        )

    def profile(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "official_url": "https://ollama.com/",
            "docs_url": "https://github.com/ollama/ollama/blob/main/docs/api.md",
            "api_format": "OpenAI-compatible",
            "request_url": self.base_url,
            "chat_endpoint": f"{self.base_url}/v1/chat/completions",
            "credential_env": "",
            "default_model": self.model,
            "models": [self.model],
            "capabilities": asdict(self.get_capabilities()),
            "local": True,
        }
