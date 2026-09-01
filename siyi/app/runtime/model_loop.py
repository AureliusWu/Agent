"""Model-call mechanics, separate from Runner's retry and task-state policy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class ModelPreflight:
    messages: list[dict[str, Any]]
    context: Any
    max_output_tokens: int
    reason: str | None


def prepare_model_call(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    route: Any,
    phase: str,
    token_budget: Any,
    context_budget: Callable[..., Any],
    compact: Callable[..., Any],
    checkpoint: Callable[..., Any],
    emit: Callable[..., Any],
    audit: Callable[..., Any],
    conversation_id: int,
    task_id: str,
) -> ModelPreflight:
    context = context_budget(messages, tools, model=route.model, desired_output_tokens=route.max_output_tokens)
    if context.should_compact:
        checkpoint(phase, "before_model_context_compaction")
        messages, compaction = compact(messages, tools, target_input_tokens=context.compaction_threshold_tokens)
        emit("context.compacted", {**compaction, "model_context_window": context.context_window_tokens})
        audit(conversation_id, "context_compaction", task_id, "ok", {
            **compaction, "model": route.model, "context_window": context.context_window_tokens,
        })
        context = context_budget(messages, tools, model=route.model, desired_output_tokens=route.max_output_tokens)
    allowed_by_window = max(
        0, context.context_window_tokens - context.estimated_input_tokens
        - context.provider_overhead_tokens - context.safety_margin_tokens,
    )
    maximum, reason = token_budget.preflight(
        phase, context.estimated_input_tokens, min(route.max_output_tokens, allowed_by_window),
    )
    return ModelPreflight(messages, context, maximum, reason)


async def complete_model_call(
    complete: Callable[..., Awaitable[dict[str, Any]]],
    messages: list[dict[str, Any]],
    api_key: str | None,
    *,
    model_kwargs: dict[str, Any],
    phase: str,
    round_number: int,
    timeout: float,
    event_callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Bound the wait; cancellation and provider failures propagate unchanged."""
    kwargs = dict(model_kwargs)
    if event_callback is not None:
        event_callback("model.started", {"phase": phase, "round": round_number, "model": kwargs["model"]})
        kwargs["event_callback"] = event_callback
    message = await asyncio.wait_for(complete(messages, api_key, **kwargs), timeout=timeout)
    if event_callback is not None:
        event_callback("model.completed", {"phase": phase, "round": round_number})
    return message
