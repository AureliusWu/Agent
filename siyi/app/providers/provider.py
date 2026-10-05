from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any, Callable, Literal
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx

from app.config import settings
from app.data_flow import record_data_flow
from app.database import now_iso, record_model_run
from app.kernel.errors import KernelError
from app.providers.costs import pricing_snapshot, usage_snapshot
from app.runtime.cost_budget import CostBudgetBlocked, reserve, release_unsent, check_result, mark_uncertain
from app.security.network_security import NetworkPolicyError, guarded_request, validate_outbound_url
from app.cognition.output_protocol import StreamingProtocolGuard, parse_deepseek_text_tool_calls, sanitize_unexecuted_tool_protocol
from app.cognition.reasoning_summary import PRIVATE_REASONING_KEY, safe_reasoning_summary
from app.providers.capabilities import provider_capability_matrix, record_provider_observation
from app.security.trust import redact_payload


DEEPSEEK_API_HOST = "api.deepseek.com"
DEEPSEEK_OFFICIAL_URL = "https://platform.deepseek.com"
DEEPSEEK_DOCS_URL = "https://api-docs.deepseek.com/zh-cn/"
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")


class ProviderError(KernelError):
    def __init__(
        self,
        message: str,
        error_type: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            error_type,
            component="model_provider",
            retryable=retryable,
            details=details,
        )
        self.error_type = error_type


CompatibleReasoningEffort = Literal["none", "low", "medium", "high", "max"]
_COMPATIBLE_REASONING_EFFORTS = {"none", "low", "medium", "high", "max"}
_KNOWN_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter", "function_call"}


def _safe_finish_reason(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if value in _KNOWN_FINISH_REASONS else "unknown"


def _safe_usage_details(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, int] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ):
        raw = value.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            safe[key] = raw
    return safe


def _rate_limit_error(response: Any) -> ProviderError:
    """Separate transient throttling from exhausted billing or usage quota."""
    try:
        body = response.json()
    except (TypeError, ValueError):
        body = {}
    error = body.get("error") if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    code = str(error.get("code") or error.get("type") or "").lower()
    message = str(error.get("message") or "").lower()
    quota_markers = (
        "insufficient_quota", "quota_exceeded", "billing_hard_limit", "credit_balance",
        "insufficient balance", "quota exhausted", "余额不足", "额度耗尽", "配额耗尽",
    )
    if any(marker in code or marker in message for marker in quota_markers):
        return ProviderError("模型服务配额或余额已耗尽", "quota_exhausted")
    return ProviderError("模型服务请求过于频繁", "rate_limited", retryable=True)


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
    final_segment = path.rsplit("/", 1)[-1].lower()
    is_version_root = (
        final_segment.startswith("v")
        and final_segment[1:].replace(".", "", 1).isdigit()
    )
    if _is_deepseek(resolved) or is_version_root or path.endswith("/beta"):
        return f"{resolved}/{suffix}"
    return f"{resolved}/v1/{suffix}"


def provider_profile() -> dict[str, Any]:
    request_url = _safe_public_url(settings.model_base_url)
    deepseek = _is_deepseek(request_url)
    models = set(settings.model_routes.values())
    if deepseek:
        # Advertise canonical IDs even when an existing install selects an old
        # alias. Do not migrate or conceal the user's explicit configuration.
        models.update(DEEPSEEK_MODELS)
        models.add(settings.model_name)
    models = sorted(models)
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
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderError("模型响应缺少 message", "invalid_response")
    message = dict(message)
    finish_reason = _safe_finish_reason(choice.get("finish_reason"))
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise ProviderError("模型工具调用格式无效", "invalid_tool_call")
    normalized_calls: list[dict[str, Any]] = []
    for call in tool_calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ProviderError("模型工具调用数据不完整", "invalid_tool_call")
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        if not isinstance(arguments, str):
            raise ProviderError("模型工具调用数据不完整", "invalid_tool_call")
        normalized_calls.append(
            {
                **call,
                "function": {**function, "arguments": arguments},
            }
        )
    if normalized_calls:
        message["tool_calls"] = normalized_calls
    if not message.get("content") and not tool_calls:
        safe_details = {
            "finish_reason": finish_reason or "unknown",
            "usage": _safe_usage_details(body.get("usage")),
        }
        if finish_reason == "length":
            has_reasoning = any(
                bool(message.get(key))
                for key in ("reasoning", "reasoning_content", PRIVATE_REASONING_KEY)
            )
            if has_reasoning:
                raise ProviderError(
                    "模型输出额度已用于推理，未生成最终正文",
                    "empty_after_reasoning",
                    details=safe_details,
                )
            raise ProviderError(
                "模型输出达到长度上限，未生成正文",
                "output_limit",
                details=safe_details,
            )
        raise ProviderError(
            "模型响应为空",
            "empty_response",
            retryable=True,
            details=safe_details,
        )
    if finish_reason is not None:
        message["finish_reason"] = finish_reason
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return message, usage


def _provider_protocol_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore private reasoning only at the provider protocol boundary."""

    prepared: list[dict[str, Any]] = []
    for original in messages:
        message = dict(original)
        private_reasoning = message.pop(PRIVATE_REASONING_KEY, None)
        if private_reasoning and message.get("role") == "assistant" and message.get("tool_calls"):
            message["reasoning_content"] = private_reasoning
        prepared.append(message)
    return prepared


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
    allow_private_provider: bool | None = None,
    provider_id_override: str | None = None,
    timeout_seconds: int | None = None,
    max_retries: int | None = None,
    response_format: dict[str, Any] | None = None,
    credential_policy: str = "required",
    reasoning_effort: CompatibleReasoningEffort | None = None,
) -> dict[str, Any]:
    if credential_policy not in {"required", "optional", "forbidden"}:
        raise ProviderError("Provider 凭据策略无效", "invalid_configuration")
    if reasoning_effort is not None and (
        not isinstance(reasoning_effort, str)
        or reasoning_effort not in _COMPATIBLE_REASONING_EFFORTS
    ):
        raise ProviderError("Provider 推理强度配置无效", "invalid_configuration")
    key = "" if credential_policy == "forbidden" else (api_key or settings.deepseek_api_key)
    if credential_policy == "required" and not key:
        raise ProviderError("未配置模型 API Key", "missing_api_key")
    resolved_url = (base_url or settings.model_base_url).rstrip("/")
    resolved_model = model or settings.model_name
    resolved_max_tokens = max(1, min(max_tokens or settings.model_max_tokens, settings.model_max_tokens))
    private_provider_allowed = settings.allow_private_model_provider if allow_private_provider is None else allow_private_provider
    provider_run_name = provider_id_override or _provider_name(resolved_url)
    safe_messages, sensitive = redact_payload(_provider_protocol_messages(messages))
    record_data_flow(
        source="conversation_context",
        sink=f"model_api:{provider_run_name}",
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
    if reasoning_effort is not None and not _is_deepseek(resolved_url):
        payload["reasoning_effort"] = reasoning_effort
    if tools:
        payload.update({"tools": tools, "tool_choice": "auto"})
    if response_format:
        payload["response_format"] = response_format

    async def notify(event: str, data: dict[str, Any]) -> None:
        if event_callback is None:
            return
        result = event_callback(event, data)
        if inspect.isawaitable(result):
            await result

    started_at, started = now_iso(), time.perf_counter()
    retry_count = 0
    usage: dict[str, Any] = {}
    first_token_ms: int | None = None
    finish_reason: str | None = None
    frozen_price = pricing_snapshot(provider=provider_run_name, base_url=resolved_url, model=resolved_model)
    sent_attempts = 0
    cost_reservation = None
    cost_budget_task_id = None
    persisted_metrics = None
    persistence_attempted = False

    def persist(success: bool, error_type: str | None, *, observed_streaming: bool | None = None, observed_tool_calls: bool | None = None) -> dict[str, Any]:
        nonlocal cost_reservation, persisted_metrics, persistence_attempted
        if persisted_metrics is not None:
            return persisted_metrics
        persistence_attempted = True
        snapshot = usage_snapshot(frozen_price, usage, attempts=max(1, sent_attempts),
                                  successful=success, request_sent=sent_attempts > 0)
        known_cost = float(snapshot["known_cost_usd_decimal"])
        estimated_cost = known_cost if snapshot["cost_status"] == "known" else None
        metrics = {
            "provider": provider_run_name,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "first_token_ms": first_token_ms,
            "finish_reason": finish_reason,
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
            "cost_status": snapshot["cost_status"],
            "known_cost_usd": known_cost,
            "unknown_cost_requests": int(snapshot["cost_status"] != "known"),
            "context_window_tokens": context_window_tokens,
            "reserved_output_tokens": reserved_output_tokens,
            "estimated_input_tokens": estimated_input_tokens,
            "input_estimate": True,
        }
        record_model_run(
            conversation_id=conversation_id,
            task_id=task_id,
            provider=provider_run_name,
            model=resolved_model,
            started_at=started_at,
            duration_ms=metrics["latency_ms"],
            first_token_ms=first_token_ms,
            usage=usage,
            success=success,
            error_type=error_type,
            retry_count=retry_count,
            phase=phase,
            route_tier=route_tier,
            task_type=task_type,
            route_confidence=route_confidence,
            max_output_tokens=resolved_max_tokens,
            estimated_cost_usd=known_cost,
            context_window_tokens=context_window_tokens,
            reserved_output_tokens=reserved_output_tokens,
            estimated_input_tokens=estimated_input_tokens,
            input_estimate=True,
            price_snapshot=snapshot,
            cost_reservation=cost_reservation,
        )
        cost_reservation = None
        persisted_metrics = metrics
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

    def check_bounded_result(metrics: dict[str, Any]) -> None:
        if cost_budget_task_id is not None:
            try:
                check_result(cost_budget_task_id, metrics)
            except CostBudgetBlocked as exc:
                raise ProviderError(str(exc), exc.code) from exc

    resolved_timeout = max(1, min(timeout_seconds or settings.model_timeout_seconds, 600))
    resolved_max_retries = max(0, min(settings.model_max_retries if max_retries is None else max_retries, 5))
    # Estimate the final credential-redacted/protocol payload, including private
    # reasoning, tool schemas and structured-response schema. This is not a
    # provider tokenizer or a guarantee of an eventual invoice amount.
    from app.efficiency import estimate_model_input_tokens
    schema_overhead = len(json.dumps(response_format or {}, ensure_ascii=False).encode("utf-8"))
    input_estimate = max(estimated_input_tokens, estimate_model_input_tokens(safe_messages, tools)) + schema_overhead
    unsupported_input = any(isinstance(message.get("content"), list)
                            and any(not isinstance(part, dict) or part.get("type") not in {"text", "input_text"}
                                    for part in message["content"]) for message in safe_messages)
    try:
        cost_reservation = reserve(task_id=task_id, price=frozen_price, input_tokens=input_estimate,
                                   output_tokens=resolved_max_tokens, unsupported_input=unsupported_input)
    except CostBudgetBlocked as exc:
        raise ProviderError(str(exc), exc.code) from exc
    if cost_reservation is not None:
        cost_budget_task_id = cost_reservation.task_id
        # A failed attempt may incur unreported usage. Dollar-constrained calls
        # therefore never retry implicitly; the user's default remains intact.
        resolved_max_retries = 0
    timeout = httpx.Timeout(resolved_timeout, connect=min(settings.model_connect_timeout_seconds, resolved_timeout))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for attempt in range(resolved_max_retries + 1):
                retry_count = attempt
                finish_reason = None
                try:
                    if event_callback is not None:
                        stream_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
                        endpoint = _provider_endpoint(resolved_url, "chat/completions")
                        await validate_outbound_url(
                            endpoint,
                            purpose="model_provider",
                            allow_private=private_provider_allowed,
                        )
                        message: dict[str, Any] = {"role": "assistant", "content": ""}
                        streamed_tools: dict[int, dict[str, Any]] = {}
                        streamed_bytes = 0
                        emitted_delta = False
                        delta_buffer = ""
                        protocol_guard = StreamingProtocolGuard()
                        last_delta_emit = time.monotonic()
                        reasoning_summary_emitted = False
                        request_headers = {"Content-Type": "application/json"}
                        if key:
                            request_headers["Authorization"] = f"Bearer {key}"
                        sent_attempts += 1
                        async with client.stream(
                            "POST",
                            endpoint,
                            headers=request_headers,
                            json=stream_payload,
                        ) as response:
                            if response.status_code in {401, 403}:
                                raise ProviderError("模型 API Key 无效或没有访问权限", "authentication")
                            if response.status_code == 404:
                                raise ProviderError("模型或接口不存在", "model_not_found")
                            if response.status_code == 429:
                                raise _rate_limit_error(response)
                            if response.status_code == 413:
                                raise ProviderError("模型请求超过上下文容量", "context_overflow")
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
                                choice = choices[0]
                                observed_finish = _safe_finish_reason(choice.get("finish_reason"))
                                if observed_finish is not None:
                                    finish_reason = observed_finish
                                delta = choice.get("delta") or {}
                                content_delta = delta.get("content")
                                if isinstance(content_delta, str) and content_delta:
                                    if first_token_ms is None:
                                        first_token_ms = round((time.perf_counter() - started) * 1000)
                                    message["content"] += content_delta
                                    safe_delta = protocol_guard.feed(content_delta)
                                    emitted_delta = emitted_delta or bool(safe_delta)
                                    delta_buffer += safe_delta
                                    if len(delta_buffer) >= 48 or time.monotonic() - last_delta_emit >= 0.05:
                                        await notify("model.delta", {"delta": delta_buffer, "phase": phase})
                                        delta_buffer = ""
                                        last_delta_emit = time.monotonic()
                                reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
                                if isinstance(reasoning_delta, str) and reasoning_delta:
                                    if first_token_ms is None:
                                        first_token_ms = round((time.perf_counter() - started) * 1000)
                                    message[PRIVATE_REASONING_KEY] = str(message.get(PRIVATE_REASONING_KEY) or "") + reasoning_delta
                                    if not reasoning_summary_emitted:
                                        await notify(
                                            "reasoning.summary",
                                            {"phase": phase, "summary": safe_reasoning_summary(phase)},
                                        )
                                        reasoning_summary_emitted = True
                                for call_delta in delta.get("tool_calls") or []:
                                    if first_token_ms is None:
                                        first_token_ms = round((time.perf_counter() - started) * 1000)
                                    index = int(call_delta.get("index") or 0)
                                    target = streamed_tools.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                    if call_delta.get("id"):
                                        target["id"] += str(call_delta["id"])
                                    function_delta = call_delta.get("function") or {}
                                    target["function"]["name"] += str(function_delta.get("name") or "")
                                    arguments_delta = function_delta.get("arguments")
                                    if isinstance(arguments_delta, dict):
                                        arguments_delta = json.dumps(
                                            arguments_delta,
                                            ensure_ascii=False,
                                            separators=(",", ":"),
                                        )
                                    target["function"]["arguments"] += str(arguments_delta or "")
                        delta_buffer += protocol_guard.finish()
                        if delta_buffer:
                            await notify("model.delta", {"delta": delta_buffer, "phase": phase})
                        if streamed_tools:
                            message["tool_calls"] = [streamed_tools[index] for index in sorted(streamed_tools)]
                        elif _is_deepseek(resolved_url):
                            message["content"], parsed_calls = parse_deepseek_text_tool_calls(message["content"], tools)
                            if parsed_calls:
                                message["tool_calls"] = parsed_calls
                        if protocol_guard.blocked and not message.get("tool_calls"):
                            message["content"] = sanitize_unexecuted_tool_protocol(message["content"])
                            await notify("model.delta", {"delta": f"\n\n{message['content']}", "phase": phase})
                        if not message["content"]:
                            message["content"] = None
                        message, usage = _validate_message(
                            {
                                "choices": [{"message": message, "finish_reason": finish_reason}],
                                "usage": usage,
                            }
                        )
                        message["_metrics"] = persist(True, None, observed_streaming=True, observed_tool_calls=True if message.get("tool_calls") else None)
                        check_bounded_result(message["_metrics"])
                        return message

                    request_headers = {"Content-Type": "application/json"}
                    if key:
                        request_headers["Authorization"] = f"Bearer {key}"
                    await validate_outbound_url(_provider_endpoint(resolved_url, "chat/completions"),
                                                purpose="model_provider", allow_private=private_provider_allowed)
                    sent_attempts += 1
                    response = await guarded_request(
                        client,
                        "POST",
                        _provider_endpoint(resolved_url, "chat/completions"),
                        purpose="model_provider",
                        headers=request_headers,
                        json=payload,
                        allow_private=private_provider_allowed,
                    )
                    if response.status_code in {401, 403}:
                        raise ProviderError("模型 API Key 无效或没有访问权限", "authentication")
                    if response.status_code == 404:
                        raise ProviderError("模型或接口不存在", "model_not_found")
                    if response.status_code == 429:
                        raise _rate_limit_error(response)
                    if response.status_code == 413:
                        raise ProviderError("模型请求超过上下文容量", "context_overflow")
                    if response.status_code >= 500:
                        raise ProviderError("模型服务端错误", "server_error", retryable=True)
                    if response.status_code >= 400:
                        raise ProviderError(f"模型请求参数错误（HTTP {response.status_code}）", "invalid_request")
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise ProviderError("模型响应 JSON 无法解析", "invalid_json") from exc
                    raw_choices = body.get("choices") if isinstance(body, dict) else None
                    if isinstance(raw_choices, list) and raw_choices and isinstance(raw_choices[0], dict):
                        finish_reason = _safe_finish_reason(raw_choices[0].get("finish_reason"))
                    message, usage = _validate_message(body)
                    private_reasoning = message.pop("reasoning_content", None) or message.pop("reasoning", None)
                    if private_reasoning:
                        message[PRIVATE_REASONING_KEY] = str(private_reasoning)
                    if not message.get("tool_calls") and _is_deepseek(resolved_url):
                        message["content"], parsed_calls = parse_deepseek_text_tool_calls(
                            str(message.get("content") or ""), tools
                        )
                        if parsed_calls:
                            message["tool_calls"] = parsed_calls
                    message["content"] = sanitize_unexecuted_tool_protocol(
                        str(message.get("content") or ""),
                        has_native_tool_calls=bool(message.get("tool_calls")),
                    ) or None
                    message["_metrics"] = persist(True, None, observed_tool_calls=True if message.get("tool_calls") else None)
                    check_bounded_result(message["_metrics"])
                    return message
                except ProviderError as exc:
                    if exc.retryable and attempt < resolved_max_retries:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                    persist(False, exc.error_type)
                    raise
                except NetworkPolicyError as exc:
                    record_data_flow(
                        source="agent_runtime",
                        sink=f"model_api:{provider_run_name}",
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
                    if attempt < resolved_max_retries:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                    error_type = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"
                    persist(False, error_type)
                    raise ProviderError(f"模型服务连接失败：{type(exc).__name__}", error_type, retryable=True) from exc
    except asyncio.CancelledError:
        try:
            persist(False, "cancelled")
        except Exception as persistence_error:
            mark_uncertain(cost_reservation)
            raise ProviderError("取消请求的费用结算失败，预留保持未确认，已停止后续执行。", "cost_usage_unknown") from persistence_error
        raise
    except BaseException as exc:
        # If no request was sent, a pre-network error does not consume a
        # reservation. After a send, failed persistence leaves the reservation
        # intact; resume cannot silently reset that uncertain charge.
        if sent_attempts > 0 and persistence_attempted and persisted_metrics is None:
            mark_uncertain(cost_reservation)
            raise ProviderError("模型请求费用结算失败，预留保持未确认，已停止后续执行。", "cost_usage_unknown") from exc
        if sent_attempts > 0 and not persistence_attempted:
            try:
                persist(False, "unexpected_error")
            except Exception as persistence_error:
                mark_uncertain(cost_reservation)
                raise ProviderError("模型请求费用结算失败，预留保持未确认，已停止后续执行。", "cost_usage_unknown") from persistence_error
            if cost_budget_task_id is not None:
                raise ProviderError("模型请求异常且费用无法确认，已停止后续执行。", "cost_usage_unknown") from exc
        elif sent_attempts == 0:
            release_unsent(cost_reservation)
        raise
    raise ProviderError("模型调用失败，已达到最大重试次数", "retry_exhausted")


async def provider_health(
    api_key: str | None = None,
    *,
    base_url: str | None = None,
    model: str | None = None,
    timeout_seconds: int = 12,
    allow_private_provider: bool | None = None,
    provider_id_override: str | None = None,
    credential_policy: str = "required",
) -> dict[str, Any]:
    if credential_policy not in {"required", "optional", "forbidden"}:
        raise ProviderError("Provider 凭据策略无效", "invalid_configuration")
    key = "" if credential_policy == "forbidden" else (api_key or settings.deepseek_api_key)
    resolved_url = (base_url or settings.model_base_url).rstrip("/")
    resolved_model = model or settings.model_name
    provider_name = provider_id_override or _provider_name(resolved_url)
    if credential_policy == "required" and not key:
        return {"status": "unconfigured", "provider": provider_name, "latency_ms": None, "model": resolved_model}
    started = time.perf_counter()
    try:
        request_headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with httpx.AsyncClient(timeout=max(1, min(timeout_seconds, 60)), follow_redirects=False) as client:
            response = await guarded_request(
                client,
                "GET",
                _provider_endpoint(resolved_url, "models"),
                purpose="model_provider_health",
                headers=request_headers,
                allow_private=(
                    settings.allow_private_model_provider
                    if allow_private_provider is None
                    else allow_private_provider
                ),
            )
            response.raise_for_status()
        latency_ms = round((time.perf_counter() - started) * 1000)
        record_provider_observation(base_url=resolved_url, model=resolved_model, status="ok", latency_ms=latency_ms)
        return {
            "status": "ok",
            "provider": provider_name,
            "latency_ms": latency_ms,
            "model": resolved_model,
            "capabilities": provider_capability_matrix(base_url=resolved_url, model=resolved_model),
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)
        try:
            record_provider_observation(base_url=resolved_url, model=resolved_model, status="error", latency_ms=latency_ms, error=str(exc))
        except Exception:
            pass
        return {
            "status": "error",
            "provider": provider_name,
            "latency_ms": latency_ms,
            "model": resolved_model,
            "error": str(exc),
            "capabilities": provider_capability_matrix(base_url=resolved_url, model=resolved_model),
        }
