from __future__ import annotations

import time
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
        self._detected_context_window: int | None = None
        self._detected_reasoning: bool | None = None
        self._detected_tools: bool | None = None
        self._detected_vision: bool | None = None
        self._detected_embeddings: bool | None = None
        self._capability_source = "configured"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if tools and not self.allow_tools:
            self._require_capability("tool_call", False)
        if event_callback is not None and not self.allow_streaming:
            self._require_capability("streaming", False)
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

    async def _request_api_json(
        self,
        resource: str,
        purpose: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await guarded_request(
                    client,
                    method,
                    f"{self.base_url}{resource}",
                    purpose=purpose,
                    allow_private=True,
                    json=body,
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

    async def _get_api_json(self, resource: str, purpose: str) -> dict[str, Any]:
        return await self._request_api_json(resource, purpose)

    async def _show_model(self) -> dict[str, Any]:
        return await self._request_api_json(
            "/api/show",
            "local_model_provider_capabilities",
            method="POST",
            body={"model": self.model},
        )

    def _apply_model_metadata(self, payload: dict[str, Any]) -> None:
        model_info = payload.get("model_info")
        if isinstance(model_info, dict):
            for key, raw_value in model_info.items():
                if str(key).endswith(".context_length"):
                    try:
                        value = int(raw_value)
                    except (TypeError, ValueError):
                        continue
                    if value > 0:
                        self._detected_context_window = value
                        break
        details = payload.get("details")
        finetune = str(details.get("finetune") or "") if isinstance(details, dict) else ""
        capabilities = payload.get("capabilities")
        capability_names = {
            str(item).strip().casefold()
            for item in (capabilities if isinstance(capabilities, list) else [])
            if str(item).strip()
        }
        if "thinking" in finetune.casefold() or "thinking" in capability_names:
            self._detected_reasoning = True
        elif capabilities is not None:
            self._detected_reasoning = False
        if capabilities is not None:
            self._detected_tools = "tools" in capability_names
            self._detected_vision = "vision" in capability_names
            self._detected_embeddings = bool({"embedding", "embeddings"} & capability_names)
        self._capability_source = "ollama_api_show"

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
            if self.model in installed:
                self._apply_model_metadata(await self._show_model())
            common = {
                "provider": self.id,
                "model": self.model,
                "version": version,
                "models": models,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "first_load_hint": "模型首次加载可能需要更长时间，出现首个 Token 后会恢复正常速度。",
                "capabilities": self.capabilities(),
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
            streaming=self.allow_streaming,
            native_tool_calls=(
                self.allow_tools
                if self._detected_tools is None
                else self.allow_tools and self._detected_tools
            ),
            structured_output=True,
            local=True,
            vision=self._detected_vision if self._detected_vision is not None else False,
            reasoning=self._detected_reasoning,
            json_mode=True,
            embeddings=self._detected_embeddings if self._detected_embeddings is not None else False,
            context_window=self._detected_context_window,
            default_max_output_tokens=self.max_tokens,
            source=self._capability_source,
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
            "capabilities": {
                "chat": True,
                "streaming": True,
                "native_tool_calls": True,
                "structured_output": True,
                "cancellation": True,
                "local": True,
            },
            "local": True,
        }
