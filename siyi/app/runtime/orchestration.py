"""Workspace-free conversation orchestration extracted from the runtime runner."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Protocol

from app.cognition.reasoning_summary import PRIVATE_REASONING_KEY, safe_reasoning_summary
from app.config import settings
from app.context.assembler import assemble_context
from app.context.budget import compact_messages_deterministically, context_window_reason, request_budget
from app.context.compiler import compile_task_context
from app.database import now_iso
from app.efficiency import TokenBudget, compact_tool_result
from app.kernel.services import KernelServices
from app.personality.agent_profiles import filter_profile_tools
from app.personality.identity_guard import enforce_identity, inspect_identity_claim, repair_instruction
from app.providers.model_routing import apply_manual_override, classify_task
from app.runtime.execution_segments import SegmentSnapshot, finish_segment, start_segment
from app.runtime.finalization import FinalizationCallbacks, completed_result, persist_response, stopped_result as _stopped_result
from app.runtime.queue_service import QueueItem, legacy_consumed_steering, pending_steering
from app.runtime.recovery import SteeringCheckpointConflict, create_checkpoint
from app.runtime.recovery_policy import json_object as _json_object
from app.runtime.task_budget import TaskBudgetContract
from app.runtime.task_state import TaskStatus
from app.runtime.tool_loop import advertised_tool_names
from app.schemas import ChatRequest
from app.security.local_only import local_only_policy
from app.security.trust import secure_untrusted_payload
from app.tools.registry import ToolValidationError, select_model_tools, validate_arguments
from app.tools.scheduler import ToolScheduler


CompletionCallable = Callable[..., Awaitable[dict[str, Any]]]
EventCallback = Callable[[str, dict[str, Any]], Any]


class WorkspaceFreeLimits(Protocol):
    max_agent_rounds: int
    max_phase_tokens: int
    max_model_call_tokens: int
    max_tool_calls: int
    max_tool_result_chars: int
    max_file_snippet_chars: int
    max_consecutive_failures: int


def _guidance_prompt(items: tuple[QueueItem, ...]) -> str:
    return "[管理员运行中引导]\n" + "\n\n".join(item.content for item in items)


async def run_workspace_free_conversation(
    payload: ChatRequest,
    api_key: str | None,
    *,
    task_id: str,
    services: KernelServices,
    complete: CompletionCallable,
    runtime_limits: WorkspaceFreeLimits,
    budget_contract: TaskBudgetContract,
    agent_profile: Any,
    finalization_callbacks: FinalizationCallbacks,
    event_callback: EventCallback | None,
    search_credentials: dict[str, str] | None,
    checkpoint_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route = apply_manual_override(classify_task(payload.content), preferred_model=payload.preferred_model, reasoning_effort=payload.reasoning_effort)
    limit = budget_contract.token_budget_limit
    existing = services.tasks.task(task_id) or {}
    budget = TokenBudget(
        limit,
        min(limit, runtime_limits.max_phase_tokens),
        runtime_limits.max_model_call_tokens,
        total_tokens=int(existing.get("total_tokens") or 0),
        input_tokens=int(existing.get("input_tokens") or 0),
        output_tokens=int(existing.get("output_tokens") or 0),
        cached_input_tokens=int(existing.get("cached_input_tokens") or 0),
        uncached_input_tokens=int(existing.get("uncached_input_tokens") or 0),
        cache_write_tokens=int(existing.get("cache_write_tokens") or 0),
        phase_tokens=_json_object(existing.get("phase_tokens")),
        hard_limit=budget_contract.token_budget_mode == "hard",
    )
    estimated_cost_usd = float(existing.get("estimated_cost_usd") or 0)
    compiled = compile_task_context(
        task_id,
        current={"user_task": payload.content, "phase": "conversation", "step": "respond"},
        working={
            "goal": payload.content,
            "completed_steps": [],
            "pending_steps": ["respond"],
            "constraints": ["无工作区对话不得声称已读取或修改本地文件"],
            "verification": {},
        },
        decisions=("当前请求按无工作区对话执行",),
    )
    assembly = assemble_context(
        profile_context=agent_profile.system_prompt,
        task_context="当前是无工作区对话：可以回答、分析和规划，但不得声称已读取或修改本地文件。",
        conversation_id=payload.conversation_id,
        task_id=task_id,
        model=route.model,
        compiled_task_context=compiled.text,
    )
    has_checkpoint = checkpoint_state is not None
    restored = checkpoint_state or {}
    steering_batches = [item for item in restored.get("workspace_free_steering", []) if isinstance(item, str)]
    if payload.resume and "workspace_free_steering" not in restored:
        # A pre-upgrade task may already have consumed guidance without a
        # checkpoint receipt. Reuse that immutable queue record, not a UI row.
        legacy_items = tuple(legacy_consumed_steering(task_id))
        if legacy_items:
            steering_batches.append(_guidance_prompt(legacy_items))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": assembly.text},
        *services.context.history(payload.conversation_id, exclude_steering_for_task_id=task_id),
        *({"role": "user", "content": item} for item in steering_batches),
    ]
    configured_search = bool(
        (search_credentials or {}).get("tavily")
        or (search_credentials or {}).get("brave")
        or settings.tavily_api_key
        or settings.brave_api_key
    )
    network_tools = [
        tool for tool in filter_profile_tools(select_model_tools(payload.content, (), []), agent_profile)
        if str((tool.get("function") or {}).get("name") or "") in {"web_search", "web_fetch"}
    ]
    if local_only_policy().enabled:
        network_tools = []
    if not configured_search:
        network_tools = [tool for tool in network_tools if (tool.get("function") or {}).get("name") != "web_search"]

    content = ""
    reasoning_parts: list[str] = []
    model_calls = int(existing.get("model_calls") or 0)
    tool_call_count = max(int(restored.get("tool_calls") or 0), int(existing.get("tool_calls") or 0))
    round_number = model_calls
    segment_rounds = 0
    segment_tools = 0
    timeout_failures = 0
    identity_repair = False

    def segment_snapshot(summary: str) -> SegmentSnapshot:
        return SegmentSnapshot(
            phase="conversation",
            completed_steps=(f"model_rounds:{model_calls}", f"tool_calls:{tool_call_count}"),
            context_summary=summary,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
            total_tokens=budget.total_tokens,
            model_calls=model_calls,
            tool_calls=tool_call_count,
        )

    def usage_fields() -> dict[str, Any]:
        return {
            "model_calls": model_calls,
            "tool_calls": tool_call_count,
            "total_tokens": budget.total_tokens,
            "input_tokens": budget.input_tokens,
            "output_tokens": budget.output_tokens,
            "cached_input_tokens": budget.cached_input_tokens,
            "uncached_input_tokens": budget.uncached_input_tokens,
            "cache_write_tokens": budget.cache_write_tokens,
            "phase_tokens": budget.phase_tokens,
            "estimated_cost_usd": estimated_cost_usd,
        }

    segment = start_segment(task_id, "workspace_free_start", segment_snapshot("workspace-free conversation"))

    def save_checkpoint(
        reason: str,
        summary: str,
        *,
        steering_message: str | None = None,
        steering_item_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        nonlocal has_checkpoint
        state = {
            "goal": payload.content,
            **usage_fields(),
            "completed_steps": [f"model_rounds:{model_calls}", f"tool_calls:{tool_call_count}"],
            "pending_steps": ["respond"],
            "context_summary": summary,
            "working_memory": compiled.state,
            "workspace_free_steering": [*steering_batches, steering_message] if steering_message is not None else steering_batches,
        }
        if steering_item_ids:
            state["steering_item_ids"] = steering_item_ids
        checkpoint = create_checkpoint(task_id, "", "conversation", reason, state, steering_item_ids=steering_item_ids)
        has_checkpoint = True
        return checkpoint

    def apply_steering(safe_point: str) -> bool:
        for attempt in range(2):
            items = tuple(pending_steering(task_id))
            if not items:
                return False
            item_ids = tuple(item.id for item in items)
            guidance = _guidance_prompt(items)
            try:
                checkpoint = save_checkpoint(
                    "steering_applied", "已采纳运行中引导", steering_message=guidance, steering_item_ids=item_ids,
                )
            except SteeringCheckpointConflict:
                if attempt == 0:
                    continue
                raise
            steering_batches.append(guidance)
            messages.append({"role": "user", "content": guidance})
            if event_callback is not None:
                sequence = checkpoint["sequence"]
                event_callback("checkpoint.created", {"sequence": sequence, "phase": "conversation", "reason": "steering_applied"})
                event_callback("queue.consumed", {
                    "operation": "steer", "item_ids": list(item_ids), "safe_point": safe_point,
                    "checkpoint_sequence": sequence,
                })
            return True
        return False

    def deadline_result() -> dict[str, Any]:
        reason = f"任务已到达绝对截止时间 {budget_contract.task_deadline_at}"
        finish_segment(segment["id"], task_id, "stopped", "task_deadline", segment_snapshot(reason))
        services.tasks.update_task(
            task_id,
            TaskStatus.TIMED_OUT,
            termination_reason=reason,
            current_step="task_deadline",
            resumable=0,
            finished_at=now_iso(),
            **usage_fields(),
        )
        return _stopped_result(
            task_id,
            TaskStatus.TIMED_OUT,
            reason,
            tool_calls=tool_call_count,
            files_modified=0,
        )

    def budget_stop(reason: str, step: str) -> dict[str, Any]:
        finish_segment(segment["id"], task_id, "stopped", step, segment_snapshot(reason))
        services.tasks.update_task(
            task_id,
            TaskStatus.PARTIALLY_COMPLETED,
            termination_reason=reason,
            current_step=step,
            **usage_fields(),
        )
        return _stopped_result(
            task_id,
            TaskStatus.PARTIALLY_COMPLETED,
            reason,
            tool_calls=tool_call_count,
            files_modified=0,
        )

    def context_wait(code: str) -> dict[str, Any]:
        reason = context_window_reason(code)
        save_checkpoint(code, reason)
        finish_segment(segment["id"], task_id, "stopped", code, segment_snapshot(reason))
        services.tasks.update_task(
            task_id, TaskStatus.WAITING_PROVIDER, termination_reason=reason,
            current_step=code, resumable=1, paused_at=now_iso(), **usage_fields(),
        )
        return _stopped_result(task_id, TaskStatus.WAITING_PROVIDER, reason, tool_calls=tool_call_count, files_modified=0)

    async def rollover(reason: str) -> None:
        nonlocal messages, segment, segment_rounds, segment_tools
        save_checkpoint(f"before_{reason}", reason)
        finish_segment(segment["id"], task_id, "completed", reason, segment_snapshot(reason))
        context_plan = request_budget(messages, None, model=route.model, desired_output_tokens=route.max_output_tokens)
        messages, compaction = compact_messages_deterministically(
            messages, None, target_input_tokens=context_plan.compaction_threshold_tokens
        )
        if event_callback is not None:
            event_callback("context.compaction.completed", {**compaction, "reason": reason})
        segment = start_segment(task_id, reason, segment_snapshot(reason))
        segment_rounds = 0
        segment_tools = 0

    while True:
        deadline_remaining = budget_contract.deadline_remaining_seconds()
        if deadline_remaining is not None and deadline_remaining <= 0:
            return deadline_result()
        cost_reason = budget_contract.cost_budget_reason(estimated_cost_usd, before_call=True)
        if cost_reason:
            return budget_stop(cost_reason, "cost_budget_limit")
        if apply_steering("before_model"):
            identity_repair = False
            reasoning_parts.clear()
        tools_for_round = None if identity_repair else network_tools or None
        repair_suffix = [
            {"role": "assistant", "content": content},
            {"role": "user", "content": repair_instruction()},
        ] if identity_repair else []
        call_messages = [*messages, *repair_suffix]
        context_plan = request_budget(call_messages, tools_for_round, model=route.model, desired_output_tokens=route.max_output_tokens)
        if context_plan.blocked_reason:
            return context_wait(context_plan.blocked_reason)
        if context_plan.should_compact or context_plan.exceeds_context_window:
            await rollover("context_window_pressure")
            call_messages = [*messages, *repair_suffix]
            context_plan = request_budget(call_messages, tools_for_round, model=route.model, desired_output_tokens=route.max_output_tokens)
        if context_plan.blocked_reason or context_plan.exceeds_context_window:
            return context_wait(context_plan.blocked_reason or "context_window_exceeded")
        estimated_input = context_plan.estimated_input_tokens
        allowed_by_window = max(
            0,
            context_plan.context_window_tokens
            - estimated_input
            - context_plan.provider_overhead_tokens
            - context_plan.safety_margin_tokens,
        )
        usage_phase = "identity_repair" if identity_repair else "conversation"
        max_tokens, reason = budget.preflight(
            usage_phase, estimated_input, min(route.max_output_tokens, allowed_by_window, context_plan.reserved_output_tokens)
        )
        if reason:
            return budget_stop(reason, "token_budget_limit")

        if not has_checkpoint:
            # The first provider request can fail before steering, rollover or
            # tools produce a checkpoint. Persist a safe baseline under the
            # current lease before exposing that request to interruption.
            save_checkpoint("before_first_model", "等待无工作区对话的首次模型回复")

        round_number += 1
        segment_rounds += 1
        admitted_tools = advertised_tool_names(tools_for_round or [])
        kwargs: dict[str, Any] = {
            "tools": tools_for_round,
            "model": route.model,
            "max_tokens": max_tokens,
            "phase": "conversation",
            "route_tier": route.tier,
            "reasoning_effort": None if payload.reasoning_effort == "auto" else payload.reasoning_effort,
            "task_type": "response",
            "route_confidence": route.confidence,
            "conversation_id": payload.conversation_id,
            "task_id": task_id,
            "context_window_tokens": context_plan.context_window_tokens,
            "reserved_output_tokens": context_plan.reserved_output_tokens,
            "estimated_input_tokens": estimated_input,
        }
        if event_callback is not None:
            event_callback("model.started", {"phase": "conversation", "round": round_number, "model": route.model})
            kwargs["event_callback"] = event_callback
        wait_seconds, wait_limit = budget_contract.wait_timeout(budget_contract.segment_timeout_seconds)
        if wait_seconds <= 0:
            return deadline_result()
        try:
            message = await asyncio.wait_for(
                complete(call_messages, api_key, **kwargs), timeout=wait_seconds
            )
            timeout_failures = 0
        except TimeoutError:
            if wait_limit == "task_deadline" and (budget_contract.deadline_remaining_seconds() or 0) <= 0.05:
                return deadline_result()
            timeout_failures += 1
            await rollover("segment_timeout")
            if timeout_failures < runtime_limits.max_consecutive_failures:
                continue
            reason = f"模型连续 {timeout_failures} 个执行分段没有取得进展"
            finish_segment(segment["id"], task_id, "stopped", "no_progress", segment_snapshot(reason))
            services.tasks.update_task(task_id, TaskStatus.PARTIALLY_COMPLETED, termination_reason=reason, current_step="no_progress")
            return _stopped_result(task_id, TaskStatus.PARTIALLY_COMPLETED, reason, tool_calls=tool_call_count, files_modified=0)

        metrics = message.pop("_metrics", {})
        token_reason = budget.record(usage_phase, metrics.get("usage") or {})
        estimated_cost_usd = round(estimated_cost_usd + float(metrics.get("estimated_cost_usd") or 0), 8)
        model_calls += 1
        services.tasks.update_task(task_id, TaskStatus.RUNNING, current_step="model_completed", **usage_fields())
        if event_callback is not None:
            event_callback("usage.updated", budget.snapshot())
            event_callback("model.completed", {"phase": "conversation", "round": round_number})
        if token_reason:
            return budget_stop(token_reason, "token_budget_limit")
        cost_reason = budget_contract.cost_budget_reason(estimated_cost_usd)
        if cost_reason:
            return budget_stop(cost_reason, "cost_budget_limit")
        if apply_steering("after_model"):
            # Neither a superseded answer nor its unstarted tools belong in
            # the new request. Re-enter the normal deadline/usage preflight.
            content = ""
            identity_repair = False
            reasoning_parts.clear()
            if segment_rounds >= runtime_limits.max_agent_rounds:
                await rollover("round_boundary")
            continue
        native_reasoning = str(message.get(PRIVATE_REASONING_KEY) or "")
        if identity_repair:
            content = str(message.get("content") or "")
            if native_reasoning:
                reasoning_parts = [safe_reasoning_summary("repair")]
            guard = inspect_identity_claim(content)
            if not guard.passed:
                content = enforce_identity(content)
                services.trace.audit(payload.conversation_id, "identity_guard", task_id, "enforced", guard.as_dict())
            break
        if native_reasoning:
            reasoning_parts.append(safe_reasoning_summary("conversation"))
        tool_calls = list(message.get("tool_calls") or [])
        if not tool_calls:
            content = str(message.get("content") or "")
            guard = inspect_identity_claim(content)
            if not guard.passed:
                services.trace.audit(payload.conversation_id, "identity_guard", task_id, "repair", guard.as_dict())
                identity_repair = True
                if segment_rounds >= runtime_limits.max_agent_rounds:
                    await rollover("round_boundary")
                continue
            break

        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            **({PRIVATE_REASONING_KEY: native_reasoning} if native_reasoning else {}),
            "tool_calls": tool_calls,
        })

        async def invoke(call: dict[str, Any]) -> dict[str, Any]:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            started, started_perf = now_iso(), time.perf_counter()
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if name not in {"web_search", "web_fetch"}:
                    raise ToolValidationError(f"无工作区模式不允许工具：{name}")
                validate_arguments(name, arguments)
                outcome = await services.tools.execute(
                    workspace="",
                    mode="full",
                    name=name,
                    arguments=arguments,
                    tool_call_id=str(call.get("id") or ""),
                    approved_actions=[],
                    approval_scope="once",
                    conversation_id=payload.conversation_id,
                    task_id=task_id,
                    mcp_routes={},
                    allow_local_mcp=False,
                    search_credentials=search_credentials,
                    admitted_tool_names=admitted_tools,
                )
                result = outcome.result
                confirmed, risk, source = outcome.confirmed, outcome.risk, outcome.source
            except (ValueError, TypeError, ToolValidationError) as exc:
                result = {"success": False, "status": "error", "error_code": "invalid_tool_call", "error_message": str(exc)}
                confirmed, risk, source = False, "low", "builtin"
            services.tasks.record_tool_run(
                conversation_id=payload.conversation_id,
                task_id=task_id,
                tool=name,
                arguments=arguments if "arguments" in locals() else {},
                result=result,
                started=started,
                started_perf=started_perf,
                risk=risk,
                confirmed=confirmed,
                source=source,
            )
            return result

        scheduler = ToolScheduler(task_id, max_parallel=settings.max_parallel_tool_calls)
        scheduled = await scheduler.execute(tool_calls, invoke)
        for item in scheduled:
            name = str(((tool_calls[item.index].get("function") or {}).get("name")) or "")
            tool_call_count += 1
            segment_tools += 1
            model_result = compact_tool_result(
                name,
                item.result,
                max_chars=runtime_limits.max_tool_result_chars,
                file_chars=runtime_limits.max_file_snippet_chars,
            )
            secured_result, sensitive, findings = secure_untrusted_payload(model_result, f"tool:{name}")
            services.trace.data_flow(
                source=f"tool:{name}", sink="model_context", classification=sensitive.classification,
                fields=("tool_result",), redactions=sensitive.redactions, allowed=True,
                reason=f"untrusted tool output; injection findings: {','.join(findings)}" if findings else "untrusted tool output",
                conversation_id=payload.conversation_id, task_id=task_id,
            )
            messages.append({"role": "tool", "tool_call_id": item.call_id, "content": json.dumps(secured_result, ensure_ascii=False)})
            save_checkpoint("after_tool_call", f"完成无工作区工具调用 {name}")
            if event_callback is not None:
                event_callback(
                    "progress.updated",
                    {
                        "phase": "conversation",
                        "model_calls": model_calls,
                        "tool_calls": tool_call_count,
                        "summary": f"完成工具调用 {name}",
                    },
                )

        if segment_rounds >= runtime_limits.max_agent_rounds or segment_tools >= runtime_limits.max_tool_calls:
            await rollover("round_boundary" if segment_rounds >= runtime_limits.max_agent_rounds else "tool_call_boundary")

    reasoning = "\n\n".join(reasoning_parts)
    finish_segment(segment["id"], task_id, "completed", "response_completed", segment_snapshot("response completed"))
    persist_response(
        services, finalization_callbacks, conversation_id=payload.conversation_id,
        task_id=task_id, prompt=payload.content, content=content, reasoning=reasoning,
    )
    services.tasks.update_task(task_id, TaskStatus.VERIFYING, current_step="verifying_response")
    report = services.verifier.verify_response(task_id, content)
    final_status = services.verifier.finalize(
        task_id,
        report,
        model_calls=model_calls,
        total_tokens=budget.total_tokens,
        input_tokens=budget.input_tokens,
        output_tokens=budget.output_tokens,
        cached_input_tokens=budget.cached_input_tokens,
        uncached_input_tokens=budget.uncached_input_tokens,
        cache_write_tokens=budget.cache_write_tokens,
        phase_tokens=budget.phase_tokens,
        estimated_cost_usd=estimated_cost_usd,
        model_route=route.__dict__,
        current_step="completed",
        completed_steps=["conversation_response", "verification:passed"],
        pending_steps=[],
    )
    if final_status == TaskStatus.COMPLETED:
        finalization_callbacks.schedule_title(payload.conversation_id, payload.content, content, api_key)
    return completed_result(
        services, conversation_id=payload.conversation_id, task_id=task_id,
        content=content, reasoning=reasoning, final_status=final_status,
        report=report, usage=budget.snapshot(),
    )
