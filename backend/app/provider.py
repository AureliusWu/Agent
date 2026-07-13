from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import settings
from .database import now_iso, record_model_run


class ProviderError(ValueError):
    def __init__(self, message: str, error_type: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


def _provider_name(base_url: str) -> str:
    return urlparse(base_url).netloc or "openai-compatible"


def _validate_message(body: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(body, dict):
        raise ProviderError("模型响应不是 JSON 对象", "invalid_response")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError("模型响应缺少 choices", "invalid_response")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderError("模型响应缺少 message", "invalid_response")
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise ProviderError("模型工具调用格式无效", "invalid_tool_call")
    for call in tool_calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not isinstance(function.get("arguments", "{}"), str):
            raise ProviderError("模型工具调用数据不完整", "invalid_tool_call")
    if not message.get("content") and not tool_calls:
        raise ProviderError("模型响应为空", "empty_response", retryable=True)
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return message, usage


async def completion(
    messages: list[dict[str, Any]],
    api_key: str | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
    base_url: str | None = None,
    model: str | None = None,
    conversation_id: int | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    key = api_key or settings.deepseek_api_key
    if not key:
        raise ProviderError("未配置模型 API Key", "missing_api_key")
    resolved_url = (base_url or settings.model_base_url).rstrip("/")
    resolved_model = model or settings.model_name
    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "temperature": settings.model_temperature,
        "max_tokens": settings.model_max_tokens,
    }
    if tools:
        payload.update({"tools": tools, "tool_choice": "auto"})

    started_at, started = now_iso(), time.perf_counter()
    retry_count = 0
    usage: dict[str, Any] = {}

    def persist(success: bool, error_type: str | None) -> dict[str, Any]:
        metrics = {
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "usage": usage,
            "attempts": retry_count + 1,
            "retry_count": retry_count,
        }
        record_model_run(
            conversation_id=conversation_id,
            task_id=task_id,
            provider=_provider_name(resolved_url),
            model=resolved_model,
            started_at=started_at,
            duration_ms=metrics["latency_ms"],
            usage=usage,
            success=success,
            error_type=error_type,
            retry_count=retry_count,
        )
        return metrics

    timeout = httpx.Timeout(settings.model_timeout_seconds, connect=settings.model_connect_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for attempt in range(settings.model_max_retries + 1):
                retry_count = attempt
                try:
                    response = await client.post(
                        resolved_url + "/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                    if response.status_code in {401, 403}:
                        raise ProviderError("模型 API Key 无效或没有访问权限", "authentication")
                    if response.status_code == 404:
                        raise ProviderError("模型或接口不存在", "model_not_found")
                    if response.status_code == 429:
                        raise ProviderError("模型服务请求过于频繁", "rate_limited", retryable=True)
                    if response.status_code >= 500:
                        raise ProviderError("模型服务端错误", "server_error", retryable=True)
                    if response.status_code >= 400:
                        raise ProviderError(f"模型请求参数错误（HTTP {response.status_code}）", "invalid_request")
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise ProviderError("模型响应 JSON 无法解析", "invalid_json") from exc
                    message, usage = _validate_message(body)
                    message["_metrics"] = persist(True, None)
                    return message
                except ProviderError as exc:
                    if exc.retryable and attempt < settings.model_max_retries:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                    persist(False, exc.error_type)
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt < settings.model_max_retries:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                    error_type = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"
                    persist(False, error_type)
                    raise ProviderError(f"模型服务连接失败：{type(exc).__name__}", error_type, retryable=True) from exc
    except asyncio.CancelledError:
        persist(False, "cancelled")
        raise
    raise ProviderError("模型调用失败，已达到最大重试次数", "retry_exhausted")


async def provider_health(api_key: str | None = None) -> dict[str, Any]:
    key = api_key or settings.deepseek_api_key
    if not key:
        return {"status": "unconfigured", "latency_ms": None, "model": settings.model_name}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                settings.model_base_url.rstrip("/") + "/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            response.raise_for_status()
        return {"status": "ok", "latency_ms": round((time.perf_counter() - started) * 1000), "model": settings.model_name}
    except Exception as exc:
        return {"status": "error", "latency_ms": round((time.perf_counter() - started) * 1000), "model": settings.model_name, "error": str(exc)}
