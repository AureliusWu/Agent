"""Read-only batching and model-result handling behind the registered Executor."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


def result_fingerprint(result: dict[str, Any], sanitize: Callable[[Any], Any]) -> str:
    stable = {
        "success": result.get("success"), "status": result.get("status"),
        "error_code": result.get("error_code"), "data": sanitize(result.get("data")),
    }
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SecuredToolResult:
    payload: Any
    taint_source: str | None


def secure_model_tool_result(
    name: str,
    result: dict[str, Any],
    *,
    conversation_id: int,
    task_id: str,
    max_chars: int,
    file_chars: int,
    compact: Callable[..., Any],
    secure: Callable[..., Any],
    data_flow: Callable[..., Any],
    audit: Callable[..., Any] | None = None,
) -> SecuredToolResult:
    model_result = compact(name, result, max_chars=max_chars, file_chars=file_chars)
    secured, sensitive, findings = secure(model_result, f"tool:{name}")
    data_flow(
        source=f"tool:{name}", sink="model_context", classification=sensitive.classification,
        fields=("tool_result",), redactions=sensitive.redactions, allowed=True,
        reason=f"untrusted tool output; injection findings: {','.join(findings)}" if findings else "untrusted tool output",
        conversation_id=conversation_id, task_id=task_id,
    )
    if findings and audit is not None:
        audit(conversation_id, "prompt_injection_detected", name, "blocked_as_instruction", {"findings": findings, "task_id": task_id})
    return SecuredToolResult(secured, f"tool:{name}" if findings else None)


async def prefetch_reads(
    *,
    pending_calls: list[dict[str, Any]],
    mcp_routes: dict[str, Any],
    extension_routes: dict[str, Any],
    signatures: Counter[str],
    max_duplicate_calls: int,
    max_file_chars: int,
    max_parallel: int,
    read_cache: Any,
    execute: Callable[..., Awaitable[Any]],
    workspace: str,
    task_id: str,
    conversation_id: int,
    approved_actions: list[str],
    approval_scope: str,
    permission_mode: Callable[[str], str],
    allow_local_mcp: bool,
    search_credentials: dict[str, str] | None,
    repair_attempt: int,
    retry_scope: list[str],
    record_cache: Callable[[bool], None],
    select_batch: Callable[..., list[dict[str, Any]]],
    scheduler_factory: Callable[..., Any],
    timestamp: Callable[[], str],
    perf_counter: Callable[[], float],
) -> dict[str, dict[str, Any]]:
    batch = select_batch(pending_calls, set(mcp_routes))
    if not batch:
        return {}
    prepared: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    projected: Counter[str] = Counter()
    for item in batch:
        function = item.get("function") or {}
        name = str(function.get("name") or "")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            return {}
        if name in {"read_file", "read_file_range"}:
            try:
                requested_chars = int(arguments.get("max_chars") or max_file_chars)
            except (TypeError, ValueError):
                requested_chars = max_file_chars
            arguments["max_chars"] = max(1, min(requested_chars, max_file_chars))
        signature = f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
        projected[signature] += 1
        if signatures[signature] + projected[signature] >= max_duplicate_calls:
            return {}
        prepared.append((item, name, arguments))
    prepared_by_id = {str(item.get("id") or ""): (name, arguments) for item, name, arguments in prepared}

    async def invoke(item: dict[str, Any]) -> dict[str, Any]:
        call_id = str(item.get("id") or "")
        name, arguments = prepared_by_id[call_id]
        started, started_perf = timestamp(), perf_counter()
        cached = read_cache.get_context_reference(name, arguments)
        if cached is not None:
            record_cache(True)
            return {"success": bool(cached.get("success", True)), "result": cached, "confirmed": False,
                    "risk": "low", "source": "cache", "started": started, "started_perf": started_perf}
        record_cache(False)
        source_before = read_cache.observe(name, arguments)
        outcome = await execute(
            workspace=workspace, mode=permission_mode(name), name=name, arguments=arguments,
            tool_call_id=call_id, approved_actions=approved_actions, approval_scope=approval_scope,
            conversation_id=conversation_id, task_id=task_id, mcp_routes=mcp_routes,
            extension_routes=extension_routes, allow_local_mcp=allow_local_mcp,
            search_credentials=search_credentials, repair_attempt=repair_attempt, retry_scope=retry_scope,
        )
        read_cache.set(name, arguments, outcome.result, observed_before=source_before)
        return {
            "success": bool(outcome.result.get("success")), "result": outcome.result,
            "confirmed": outcome.confirmed, "risk": outcome.risk, "source": outcome.source,
            "started": started, "started_perf": started_perf,
        }

    scheduler = scheduler_factory(task_id, max_parallel=max_parallel)
    outcomes = await scheduler.execute([item for item, _, _ in prepared], invoke)
    return {outcome.call_id: outcome.result for outcome in outcomes}
