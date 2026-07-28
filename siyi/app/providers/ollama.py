from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Callable

import httpx

from app.providers.base import FailureCategory, LLMProvider, ProviderCapabilities
from app.providers.configuration import OLLAMA_BASE_URL, OLLAMA_MODEL, ProviderConfiguration
from app.providers.provider import ProviderError, completion as transport_completion
from app.security.network_security import guarded_request


class OllamaProvider(LLMProvider):
    id = "ollama"
    name = "Ollama"

    def __init__(self, config: ProviderConfiguration) -> None:
        self.base_url = (config.base_url or OLLAMA_BASE_URL).rstrip("/")
        self.model = config.model or OLLAMA_MODEL
        self.timeout_seconds = config.timeout_seconds
        self.max_tokens = config.max_tokens
        self.max_retries = config.max_retries
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
        requested_max_tokens = int(kwargs.pop("max_tokens", self.max_tokens))
        return await transport_completion(
            messages,
            api_key="ollama-local-only",
            tools=tools if self.allow_tools else None,
            event_callback=event_callback if self.allow_streaming else None,
            base_url=self.base_url,
            model=self.model,
            max_tokens=min(max(requested_max_tokens, 2048), self.max_tokens),
            allow_private_provider=True,
            provider_id_override=self.id,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            **kwargs,
        )

    async def _get_api_json(self, resource: str, purpose: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await guarded_request(
                    client,
                    "GET",
                    f"{self.base_url}{resource}",
                    purpose=purpose,
                    allow_private=True,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise ProviderError(
                "Ollama 服务未启动。请先打开 Ollama 应用，等待服务就绪后重试。",
                "ollama_service_unavailable",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                "无法连接本机 Ollama 服务。请检查 Ollama 是否正在运行。",
                "ollama_service_unavailable",
                retryable=True,
            ) from exc
        if response.status_code != 200:
            raise ProviderError(
                f"本机 11434 端口没有返回 Ollama API（HTTP {response.status_code}）。请关闭占用该端口的其他程序并重启 Ollama。",
                "ollama_port_conflict",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "本机 11434 端口返回的不是 Ollama JSON。请检查端口是否被其他程序占用。",
                "ollama_port_conflict",
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError("Ollama 返回了无效响应，请重启 Ollama 后重试。", "ollama_invalid_response")
        return payload

    async def diagnostics(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            version_payload = await self._get_api_json("/api/version", "local_model_provider_version")
            version = str(version_payload.get("version") or "").strip()
            if not version:
                raise ProviderError(
                    "本机 11434 端口不是可识别的 Ollama 服务。请检查端口占用。",
                    "ollama_port_conflict",
                )
            tags_payload = await self._get_api_json("/api/tags", "local_model_provider_models")
            raw_models = tags_payload.get("models")
            if not isinstance(raw_models, list):
                raise ProviderError("Ollama 模型列表格式无效，请重启 Ollama 后重试。", "ollama_invalid_response")
            models = [
                {
                    "name": str(item.get("name") or item.get("model") or ""),
                    "size": max(0, int(item.get("size") or 0)),
                    "modified_at": str(item.get("modified_at") or ""),
                }
                for item in raw_models
                if isinstance(item, dict) and str(item.get("name") or item.get("model") or "")
            ]
            installed = {item["name"] for item in models}
            common = {
                "provider": self.id,
                "model": self.model,
                "version": version,
                "models": models,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "first_load_hint": "模型首次加载可能需要更长时间，出现首个 Token 后会恢复正常速度。",
                "capabilities": asdict(self.get_capabilities()),
            }
            if self.model not in installed:
                return {
                    **common,
                    "status": "error",
                    "error_type": "ollama_model_missing",
                    "error": f"本地模型未安装：{self.model}。请在终端运行 ollama pull {self.model}，司忆不会自动下载。",
                    "action": f"ollama pull {self.model}",
                    "failure_category": FailureCategory.ENVIRONMENT_FAILURE.value,
                }
            return {**common, "status": "ok", "service_status": "running"}
        except ProviderError as exc:
            return {
                "status": "error",
                "provider": self.id,
                "model": self.model,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": str(exc),
                "error_type": exc.error_type,
                "action": "打开或重启 Ollama，然后再次检查连接。",
                "failure_category": FailureCategory.ENVIRONMENT_FAILURE.value,
            }

    async def list_models(self) -> list[dict[str, Any]]:
        result = await self.diagnostics()
        return list(result.get("models") or [])

    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        return await self.diagnostics()

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
