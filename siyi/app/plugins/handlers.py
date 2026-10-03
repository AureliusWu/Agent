"""Adapters retain the established sandbox, permissions and trust boundaries."""

from __future__ import annotations

from typing import Any

from .contracts import PluginCall
from app.tools.outcomes import RuntimeToolOutcome


def permission_arguments(call: PluginCall, impact: str) -> dict[str, Any]:
    from app.tools.registry import REGISTRY

    return {
        "mode": call.mode,
        "risk": REGISTRY[call.name].risk,
        "tool": call.name,
        "arguments": call.arguments,
        "conversation_id": call.conversation_id,
        "task_id": call.task_id,
        "approval_tokens": call.approved_actions,
        "approval_scope": call.approval_scope,
        "impact": impact,
        "workspace": call.workspace,
    }


def outcome(
    call: PluginCall, result: dict[str, Any], confirmed: bool, source: str = "builtin"
) -> RuntimeToolOutcome:
    from app.tools.registry import REGISTRY

    return RuntimeToolOutcome(result, confirmed, REGISTRY[call.name].risk, source)


async def execute_workspace(call: PluginCall) -> RuntimeToolOutcome:
    from app.sandbox import execute_tool

    result = execute_tool(
        call.workspace,
        call.mode,
        call.name,
        call.arguments,
        call.approved_actions,
        approval_scope=call.approval_scope,
        conversation_id=call.conversation_id,
        task_id=call.task_id,
        tool_call_id=call.tool_call_id,
        permission_fn=call.permission_fn,
    )
    return outcome(
        call,
        result,
        bool(call.approved_actions and result.get("status") != "confirmation_required"),
    )


async def execute_process(call: PluginCall) -> RuntimeToolOutcome:
    from app.sandbox import execute_command_async

    result = await execute_command_async(
        call.workspace,
        call.mode,
        call.arguments,
        call.approved_actions,
        approval_scope=call.approval_scope,
        conversation_id=call.conversation_id,
        task_id=call.task_id,
        permission_fn=call.permission_fn,
    )
    return outcome(
        call,
        result,
        bool(call.approved_actions and result.get("status") != "confirmation_required"),
    )


async def execute_memory(call: PluginCall) -> RuntimeToolOutcome:
    from app.memory.service import execute_memory_tool

    decision = call.permission_fn(**permission_arguments(call, "当前工作区长期记忆"))
    if not decision.allowed:
        return outcome(
            call,
            decision.confirmation or {"success": False, "status": "confirmation_required"},
            decision.confirmed,
        )
    mutation = call.name in {"remember_workspace", "forget_workspace_memory"}
    blocked = mutation and (
        call.memory_write_policy == "deny"
        or (call.memory_write_policy == "explicit" and not call.memory_write_explicit)
    )
    result = (
        {
            "success": False,
            "status": "error",
            "error_code": "memory_write_policy_denied",
            "error_message": "当前任务的记忆写入策略不允许这次变更",
        }
        if blocked
        else execute_memory_tool(call.workspace, call.name, call.arguments, call.task_id or None)
    )
    return outcome(call, result, decision.confirmed)


async def execute_code(call: PluginCall) -> RuntimeToolOutcome:
    if call.name != "lsp_query":
        return await execute_workspace(call)
    from app.workspace.lsp import query_lsp

    decision = call.permission_fn(
        **permission_arguments(call, str(call.arguments.get("path") or "current workspace"))
    )
    result = (
        await query_lsp(
            call.workspace,
            str(call.arguments["path"]),
            str(call.arguments["operation"]),
            line=int(call.arguments.get("line") or 0),
            character=int(call.arguments.get("character") or 0),
            symbol=str(call.arguments.get("symbol") or ""),
        )
        if decision.allowed
        else decision.confirmation or {"success": False, "status": "confirmation_required"}
    )
    return outcome(call, result, decision.confirmed, "builtin:lsp")


async def execute_web(call: PluginCall) -> RuntimeToolOutcome:
    from app.config import settings
    from app.providers.web_search import fetch_web_page, search_web
    from app.runtime.task_events import emit_task_event

    impact = str(
        call.arguments.get("url") or f"联网搜索: {str(call.arguments.get('query', ''))[:80]}"
    )
    decision = call.permission_fn(**permission_arguments(call, impact))
    if not decision.allowed:
        return outcome(
            call,
            decision.confirmation or {"success": False, "status": "confirmation_required"},
            decision.confirmed,
            f"builtin:{call.name}",
        )
    if call.name == "web_fetch":
        result = await fetch_web_page(
            str(call.arguments["url"]), max_chars=int(call.arguments.get("max_chars") or 40_000)
        )
    else:
        query = str(call.arguments["query"]).strip()
        if not query:
            return outcome(
                call,
                {
                    "success": False,
                    "status": "error",
                    "error_code": "invalid_arguments",
                    "error_message": "搜索关键词不能为空",
                },
                decision.confirmed,
                "builtin:web_search",
            )
        provider = str(call.arguments.get("provider") or settings.default_search_provider)
        emit_task_event(call.task_id, "search.started", {"provider": provider, "query": query})
        result = await search_web(
            query,
            provider=provider,
            credentials=call.search_credentials,
            max_results=int(call.arguments.get("max_results") or 8),
            topic=str(call.arguments.get("topic") or "general"),
            time_range=str(call.arguments.get("time_range") or "") or None,
        )
        emit_task_event(
            call.task_id,
            "search.completed",
            {
                "provider": result.get("provider") or provider,
                "query": query,
                "duration_ms": result.get("duration_ms"),
                "result_count": result.get("result_count", 0),
                "success": bool(result.get("success")),
            },
        )
    return outcome(call, result, decision.confirmed, f"builtin:{call.name}")
