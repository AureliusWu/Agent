from __future__ import annotations

import json
from typing import Any


def record_model_run(
    *,
    conversation_id: int | None,
    task_id: str | None,
    provider: str,
    model: str,
    started_at: str,
    duration_ms: int,
    usage: dict[str, Any],
    success: bool,
    error_type: str | None,
    retry_count: int,
    first_token_ms: int | None = None,
    phase: str = "analysis",
    route_tier: str = "medium",
    task_type: str = "general",
    route_confidence: float = 0.0,
    max_output_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    context_window_tokens: int = 0,
    reserved_output_tokens: int = 0,
    estimated_input_tokens: int = 0,
    input_estimate: bool = False,
    price_snapshot: dict[str, Any] | None = None,
) -> None:
    from app import database as facade

    prompt_tokens = max(0, int(usage.get("prompt_tokens") or 0))
    cached_input_tokens = max(
        0,
        int(
            usage.get("prompt_cache_hit_tokens")
            or usage.get("cache_read_input_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        ),
    )
    explicit_uncached = usage.get("prompt_cache_miss_tokens")
    uncached_input_tokens = (
        max(0, int(explicit_uncached))
        if explicit_uncached is not None
        else max(0, prompt_tokens - cached_input_tokens)
    )
    cache_write_tokens = max(
        0,
        int(usage.get("cache_creation_input_tokens") or usage.get("prompt_cache_write_tokens") or 0),
    )
    with facade.connect() as db:
        if task_id:
            from app.runtime.task_leases import fence_current_task_write

            fence_current_task_write(task_id, db=db)
        db.execute(
            "INSERT INTO model_runs(conversation_id, task_id, provider, model, started_at, finished_at, duration_ms, first_token_ms, "
            "input_tokens, output_tokens, total_tokens, success, phase, route_tier, task_type, route_confidence, "
            "max_output_tokens, estimated_cost_usd, context_window_tokens, reserved_output_tokens, estimated_input_tokens, "
            "input_estimate, cached_input_tokens, uncached_input_tokens, cache_write_tokens, price_snapshot_json, "
            "error_type, retry_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                task_id,
                provider,
                model,
                started_at,
                facade.now_iso(),
                duration_ms,
                first_token_ms,
                prompt_tokens,
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                int(success),
                phase,
                route_tier,
                task_type,
                route_confidence,
                max_output_tokens,
                estimated_cost_usd,
                context_window_tokens,
                reserved_output_tokens,
                estimated_input_tokens,
                int(input_estimate),
                cached_input_tokens,
                uncached_input_tokens,
                cache_write_tokens,
                json.dumps(price_snapshot or {}, ensure_ascii=False, sort_keys=True),
                error_type,
                retry_count,
            ),
        )
