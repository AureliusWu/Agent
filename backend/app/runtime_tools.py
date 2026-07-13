from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mcp import invoke_mcp_route
from .memory import MEMORY_TOOLS, execute_memory_tool
from .permissions import authorize
from .repair import repair_tool_allowed
from .sandbox import execute_command_async, execute_tool
from .tool_registry import REGISTRY


@dataclass(frozen=True)
class RuntimeToolOutcome:
    result: dict[str, Any]
    confirmed: bool
    risk: str
    source: str


async def execute_runtime_tool(
    *,
    workspace: str,
    mode: str,
    name: str,
    arguments: dict[str, Any],
    tool_call_id: str,
    approved_actions: list[str],
    approval_scope: str,
    conversation_id: int,
    task_id: str,
    mcp_routes: dict[str, Any],
    allow_local_mcp: bool,
    repair_attempt: int = 0,
    retry_scope: list[str] | None = None,
) -> RuntimeToolOutcome:
    if repair_attempt and not repair_tool_allowed(name, retry_scope or []):
        result = {
            "success": False,
            "status": "error",
            "error_code": "repair_scope_violation",
            "error": f"限定返工不允许重复执行已通过的 {name} 步骤",
        }
        risk = REGISTRY.get(name).risk if REGISTRY.get(name) else "critical"
        return RuntimeToolOutcome(result, False, risk, "verifier")

    if name in mcp_routes:
        permission = authorize(
            mode=mode,
            risk="critical",
            tool=name,
            arguments=arguments,
            conversation_id=conversation_id,
            task_id=task_id,
            approval_tokens=approved_actions,
            approval_scope=approval_scope,
            source="mcp",
            impact="外部 MCP 服务",
        )
        if not permission.allowed:
            result = permission.confirmation or {"success": False, "status": "confirmation_required"}
        else:
            data = await invoke_mcp_route(mcp_routes[name], arguments, allow_local_mcp)
            result = {"success": True, "status": "ok", "data": data, "result": data}
        return RuntimeToolOutcome(result, permission.confirmed, "critical", "mcp")

    if name in MEMORY_TOOLS:
        spec = REGISTRY[name]
        permission = authorize(
            mode=mode,
            risk=spec.risk,
            tool=name,
            arguments=arguments,
            conversation_id=conversation_id,
            task_id=task_id,
            approval_tokens=approved_actions,
            approval_scope=approval_scope,
            impact="当前工作区长期记忆",
        )
        result = (
            execute_memory_tool(workspace, name, arguments, task_id)
            if permission.allowed
            else (permission.confirmation or {"success": False, "status": "confirmation_required"})
        )
        return RuntimeToolOutcome(result, permission.confirmed, spec.risk, "builtin")

    if name == "run_command":
        result = await execute_command_async(
            workspace,
            mode,
            arguments,
            approved_actions,
            approval_scope=approval_scope,
            conversation_id=conversation_id,
            task_id=task_id,
        )
    else:
        result = execute_tool(
            workspace,
            mode,
            name,
            arguments,
            approved_actions,
            approval_scope=approval_scope,
            conversation_id=conversation_id,
            task_id=task_id,
            tool_call_id=tool_call_id,
        )
    confirmed = bool(approved_actions and result.get("status") != "confirmation_required")
    risk = REGISTRY.get(name).risk if REGISTRY.get(name) else "critical"
    return RuntimeToolOutcome(result, confirmed, risk, "builtin")
