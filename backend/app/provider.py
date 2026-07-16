from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any, Callable
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx

from .config import settings
from .data_flow import record_data_flow
from .database import now_iso, record_model_run
from .kernel.errors import KernelError
from .model_routing import estimate_cost_usd
from .network_security import NetworkPolicyError, guarded_request, validate_outbound_url
from .output_protocol import StreamingProtocolGuard, sanitize_unexecuted_tool_protocol
from .provider_capabilities import provider_capability_matrix, record_provider_observation
from .reasoning_summary import safe_reasoning_summary, sanitize_reasoning_message
from .trust import redact_payload


DEEPSEEK_API_HOST = "api.deepseek.com"
DEEPSEEK_OFFICIAL_URL = "https://platform.deepseek.com"
DEEPSEEK_DOCS_URL = "https://api-docs.deepseek.com/zh-cn/"
DEEPSEEK_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")


class ProviderError(KernelError):
    def __init__(self, message: str, error_type: str, *, retryable: bool = False) -> None:
        super().__init__(message, error_type, component="model_provider", retryable=retryable)
        self.error_type = error_type


def _provider_name(base_url: str) -> str:
    return urlparse(base_url).netloc or "openai-compatible"


def _is_deepseek(base_url: str) -> bool:
    return (urlsplit(base_url).hostname or "").lower() == DEEPSEEK_API_HOST


def _safe_public_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def _provider_endpoint(base_url: str, resource: str) -> str:
    resolved = base_url.rstrip("/")
    suffix = resource.lstrip("/")
    parsed = urlsplit(resolved)
    path = parsed.path.rstrip("/")
    if _is_deepseek(resolved) or path.endswith("/v1") or path.endswith("/beta"):
        return f"{resolved}/{suffix}"
    return f"{resolved}/v1/{suffix}"


def provider_profile() -> dict[str, Any]:
    request_url = _safe_public_url(settings.model_base_url)
    deepseek = _is_deepseek(request_url)
    models = sorted(set(settings.model_routes.values()))
    return {
        "id": "deepseek" if deepseek else "openai-compatible",
        "name": "DeepSeek" if deepseek else "OpenAI-compatible Provider",
        "official_url": DEEPSEEK_OFFICIAL_URL if deepseek else "",
        "docs_url": DEEPSEEK_DOCS_URL if deepseek else "",
        "api_format": "OpenAI-compatible",
        "request_url": request_url,
        "chat_endpoint": _provider_endpoint(request_url, "chat/completions") if request_url else "",
        "credential_env": "AGENT_DEEPSEEK_API_KEY",
        "default_model": settings.model_name,
        "models": models,
        "thinking_modes": ["enabled", "disabled"] if deepseek else [],
        "reasoning_efforts": ["high", "max"] if deepseek else [],
        "deprecated_models": ["deepseek-chat", "deepseek-reasoner"] if deepseek else [],
    }


def _apply_provider_options(payload: dict[str, Any], base_url: str, route_tier: str) -> None:
    if not _is_deepseek(base_url):
        return
    thinking = "disabled" if route_tier == "light" else "enabled"
    payload["thinking"] = {"type": thinking}
    if thinking == "enabled":
        payload.pop("temperature", None)
        payload["reasoning_effort"] = "max" if route_tier == "strong" else "high"


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
    max_tokens: int | None = None,
    phase: str = "analysis",
    route_tier: str = "medium",
    task_type: str = "general",
    route_confidence: float = 0.0,
    conversation_id: int | None = None,
    task_id: str | None = None,
    event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
    context_window_tokens: int = 0,
    reserved_output_tokens: int = 0,
    estimated_input_tokens: int = 0,
) -> dict[str, Any]:
    key = api_key or settings.deepseek_api_key
    if not key:
        raise ProviderError("未配置模型 API Key", "missing_api_key")
    resolved_url = (base_url or settings.model_base_url).rstrip("/")
    resolved_model = model or settings.model_name
    resolved_max_tokens = max(1, min(max_tokens or settings.model_max_tokens, settings.model_max_tokens))
    safe_messages, sensitive = redact_payload(messages)
    record_data_flow(
        source="conversation_context",
        sink=f"model_api:{_provider_name(resolved_url)}",
        classification=sensitive.classification,
        fields=("message_roles", "message_content", "tool_arguments"),
        redactions=sensitive.redactions,
        allowed=True,
        reason="credentials removed before provider request" if sensitive.redactions else "provider request",
        conversation_id=conversation_id,
        task_id=task_id,
    )
    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": safe_messages,
        "temperature": settings.model_temperature,
        "max_tokens": resolved_max_tokens,
    }
    _apply_provider_options(payload, resolved_url, route_tier)
    if tools:
        payload.update({"tools": tools, "tool_choice": "auto"})

    async def notify(event: str, data: dict[str, Any]) -> None:
        if event_callback is None:
            return
        result = event_callback(event, data)
        if inspect.isawaitable(result):
            await result

    started_at, started = now_iso(), time.perf_counter()
    retry_count = 0
    usage: dict[str, Any] = {}

    def persist(success: bool, error_type: str | None, *, observed_streaming: bool | None = None, observed_tool_calls: bool | None = None) -> dict[str, Any]:
        estimated_cost = estimate_cost_usd(
            resolved_model,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
        metrics = {
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "usage": usage,
            "attempts": retry_count + 1,
            "retry_count": retry_count,
            "model": resolved_model,
            "phase": phase,
            "route_tier": route_tier,
            "task_type": task_type,
            "route_confidence": route_confidence,
            "max_output_tokens": resolved_max_tokens,
            "estimated_cost_usd": estimated_cost,
            "context_window_tokens": context_window_tokens,
            "reserved_output_tokens": reserved_output_tokens,
            "estimated_input_tokens": estimated_input_tokens,
            "input_estimate": True,
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
            phase=phase,
            route_tier=route_tier,
            task_type=task_type,
            route_confidence=route_confidence,
            max_output_tokens=resolved_max_tokens,
            estimated_cost_usd=estimated_cost,
            context_window_tokens=context_window_tokens,
            reserved_output_tokens=reserved_output_tokens,
            estimated_input_tokens=estimated_input_tokens,
            input_estimate=True,
        )
        try:
            record_provider_observation(
                base_url=resolved_url,
                model=resolved_model,
                status="ok" if success else "error",
                latency_ms=metrics["latency_ms"],
                streaming=observed_streaming,
                native_tool_calls=observed_tool_calls,
                error=error_type,
            )
        except Exception:
            pass
        return metrics

    timeout = httpx.Timeout(settings.model_timeout_seconds, connect=settings.model_connect_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for attempt in range(settings.model_max_retries + 1):
                retry_count = attempt
                try:
                    if event_callback is not None:
                        stream_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
                        endpoint = _provider_endpoint(resolved_url, "chat/completions")
                        await validate_outbound_url(
                            endpoint,
                            purpose="model_provider",
                            allow_private=settings.allow_private_model_provider,
                        )
                        message: dict[str, Any] = {"role": "assistant", "content": ""}
                        streamed_tools: dict[int, dict[str, Any]] = {}
                        streamed_bytes = 0
                        emitted_delta = False
                        delta_buffer = ""
                        protocol_guard = StreamingProtocolGuard()
                        reasoning_observed = False
                        last_delta_emit = time.monotonic()
                        async with client.stream(
                            "POST",
                            endpoint,
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            json=stream_payload,
                        ) as response:
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
                            declared = response.headers.get("content-length")
                            if declared and declared.isdigit() and int(declared) > settings.network_max_response_bytes:
                                raise ProviderError("模型流式响应超过大小限制", "response_too_large")
                            async for line in response.aiter_lines():
                                streamed_bytes += len(line.encode("utf-8"))
                                if streamed_bytes > settings.network_max_response_bytes:
                                    raise ProviderError("模型流式响应超过大小限制", "response_too_large")
                                if not line.startswith("data:"):
                                    continue
                                raw = line[5:].strip()
                                if not raw or raw == "[DONE]":
                                    continue
                                try:
                                    chunk = json.loads(raw)
                                except ValueError as exc:
                                    raise ProviderError("模型流式响应 JSON 无法解析", "invalid_json") from exc
                                if isinstance(chunk.get("usage"), dict):
                                    usage = chunk["usage"]
                                choices = chunk.get("choices") or []
                                if not choices or not isinstance(choices[0], dict):
                                    continue
                                delta = choices[0].get("delta") or {}
                                content_delta = delta.get("content")
                                if isinstance(content_delta, str) and content_delta:
                                    message["content"] += content_delta
                                    safe_delta = protocol_guard.feed(content_delta)
                                    emitted_delta = emitted_delta or bool(safe_delta)
                                    delta_buffer += safe_delta
                                    if len(delta_buffer) >= 48 or time.monotonic() - last_delta_emit >= 0.05:
                                        await notify("model.delta", {"delta": delta_buffer, "phase": phase})
                                        delta_buffer = ""
                                        last_delta_emit = time.monotonic()
                                reasoning_delta = delta.get("reasoning_content")
                                if isinstance(reasoning_delta, str) and reasoning_delta:
                                    if not reasoning_observed:
                                        reasoning_observed = True
                                        summary = safe_reasoning_summary(phase)
                                        message["reasoning_content"] = summary
                                        await notify("model.reasoning.delta", {"delta": summary, "phase": phase, "public_summary": True})
                                for call_delta in delta.get("tool_calls") or []:
                                    index = int(call_delta.get("index") or 0)
                                    target = streamed_tools.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                    if call_delta.get("id"):
                                        target["id"] += str(call_delta["id"])
                                    function_delta = call_delta.get("function") or {}
                                    target["function"]["name"] += str(function_delta.get("name") or "")
                                    target["function"]["arguments"] += str(function_delta.get("arguments") or "")
                        delta_buffer += protocol_guard.finish()
                        if delta_buffer:
                            await notify("model.delta", {"delta": delta_buffer, "phase": phase})
                        if streamed_tools:
                            message["tool_calls"] = [streamed_tools[index] for index in sorted(streamed_tools)]
                        if protocol_guard.blocked and not message.get("tool_calls"):
                            message["content"] = sanitize_unexecuted_tool_protocol(message["content"])
                        if not message["content"]:
                            message["content"] = None
                        message, usage = _validate_message({"choices": [{"message": message}], "usage": usage})
                        message["_metrics"] = persist(True, None, observed_streaming=True, observed_tool_calls=True if message.get("tool_calls") else None)
                        return message

                    response = await guarded_request(
                        client,
                        "POST",
                        _provider_endpoint(resolved_url, "chat/completions"),
                        purpose="model_provider",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json=payload,
                        allow_private=settings.allow_private_model_provider,
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
                    message = sanitize_reasoning_message(message, phase)
                    message["content"] = sanitize_unexecuted_tool_protocol(
                        str(message.get("content") or ""),
                        has_native_tool_calls=bool(message.get("tool_calls")),
                    ) or None
                    message["_metrics"] = persist(True, None, observed_tool_calls=True if message.get("tool_calls") else None)
                    return message
                except ProviderError as exc:
                    if exc.retryable and attempt < settings.model_max_retries:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                    persist(False, exc.error_type)
                    raise
                except NetworkPolicyError as exc:
                    record_data_flow(
                        source="agent_runtime",
                        sink=f"model_api:{_provider_name(resolved_url)}",
                        classification="restricted",
                        fields=("request_url",),
                        allowed=False,
                        reason=str(exc),
                        conversation_id=conversation_id,
                        task_id=task_id,
                    )
                    persist(False, "network_policy")
                    raise ProviderError(f"模型服务被网络安全策略拒绝：{exc}", "network_policy") from exc
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if event_callback is not None and locals().get("emitted_delta", False):
                        error_type = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"
                        persist(False, error_type)
                        raise ProviderError("模型流式响应在输出中断开", error_type, retryable=True) from exc
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
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            response = await guarded_request(
                client,
                "GET",
                _provider_endpoint(settings.model_base_url, "models"),
                purpose="model_provider_health",
                headers={"Authorization": f"Bearer {key}"},
                allow_private=settings.allow_private_model_provider,
            )
            response.raise_for_status()
        latency_ms = round((time.perf_counter() - started) * 1000)
        record_provider_observation(base_url=settings.model_base_url, model=settings.model_name, status="ok", latency_ms=latency_ms)
        return {"status": "ok", "latency_ms": latency_ms, "model": settings.model_name, "capabilities": provider_capability_matrix()}
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)
        try:
            record_provider_observation(base_url=settings.model_base_url, model=settings.model_name, status="error", latency_ms=latency_ms, error=str(exc))
        except Exception:
            pass
        return {"status": "error", "latency_ms": latency_ms, "model": settings.model_name, "error": str(exc), "capabilities": provider_capability_matrix()}
