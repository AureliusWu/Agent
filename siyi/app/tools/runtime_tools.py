from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from app.tools.mcp import invoke_mcp_route
from app.data_flow import record_data_flow
from app.extensions.sdk import ExtensionToolRoute
from app.permissions import PermissionDecision, authorize
from app.runtime.repair import repair_tool_allowed
from app.workspace.snapshots import SnapshotError, create_security_snapshot
from app.tools.registry import REGISTRY, ToolValidationError, validate_arguments
from app.tools.outcomes import RuntimeToolOutcome
from app.plugins.contracts import PluginCall
from app.plugins.registry import PLUGIN_LIBRARY
from app.security.trust import redact_payload, secure_untrusted_payload


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
    extension_routes: dict[str, ExtensionToolRoute] | None = None,
    allow_local_mcp: bool,
    repair_attempt: int = 0,
    retry_scope: list[str] | None = None,
    memory_write_policy: str = "explicit",
    memory_write_explicit: bool = False,
    search_credentials: dict[str, str] | None = None,
    permission_fn: Callable[..., PermissionDecision] = authorize,
    available_tool_names: tuple[str, ...] | None = None,
) -> RuntimeToolOutcome:
    extension_route = (extension_routes or {}).get(name)
    canonical_name = extension_route.delegate if extension_route else name
    if repair_attempt and not repair_tool_allowed(canonical_name, retry_scope or []):
        result = {
            "success": False,
            "status": "error",
            "error_code": "repair_scope_violation",
            "error": f"限定返工不允许重复执行已通过的 {name} 步骤",
        }
        risk = REGISTRY.get(canonical_name).risk if REGISTRY.get(canonical_name) else "critical"
        return RuntimeToolOutcome(result, False, risk, "verifier")

    call = PluginCall(
        workspace=workspace,
        mode=mode,
        name=name,
        arguments=arguments,
        tool_call_id=tool_call_id,
        approved_actions=approved_actions,
        approval_scope=approval_scope,
        conversation_id=conversation_id,
        task_id=task_id,
        permission_fn=permission_fn,
        memory_write_policy=memory_write_policy,
        memory_write_explicit=memory_write_explicit,
        search_credentials=search_credentials or {},
        available_tool_names=available_tool_names,
    )

    if extension_route is not None:
        try:
            merged_arguments = extension_route.resolve_arguments(arguments)
            validate_arguments(extension_route.delegate, merged_arguments)
        except (ToolValidationError, ValueError) as exc:
            return RuntimeToolOutcome(
                {
                    "success": False,
                    "status": "error",
                    "error_code": "invalid_extension_arguments",
                    "error_message": str(exc),
                },
                False,
                extension_route.risk,
                f"extension:{extension_route.extension_id}",
            )
        permission = permission_fn(
            mode=mode,
            risk=extension_route.risk,
            tool=name,
            arguments=merged_arguments,
            conversation_id=conversation_id,
            task_id=task_id,
            approval_tokens=approved_actions,
            approval_scope=approval_scope,
            source=f"extension:{extension_route.extension_id}",
            impact=str(
                merged_arguments.get("path") or merged_arguments.get("source") or "当前工作区"
            ),
            workspace=workspace,
        )
        if not permission.allowed:
            result = permission.confirmation or {
                "success": False,
                "status": "confirmation_required",
            }
        else:
            delegated = await PLUGIN_LIBRARY.execute(
                replace(
                    call,
                    name=extension_route.delegate,
                    arguments=merged_arguments,
                    mode="full",
                    approved_actions=[],
                    approval_scope="once",
                )
            )
            result = delegated.result
        return RuntimeToolOutcome(
            result,
            permission.confirmed,
            extension_route.risk,
            f"extension:{extension_route.extension_id}",
        )

    if name in mcp_routes:
        permission = permission_fn(
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
            workspace=workspace,
        )
        if not permission.allowed:
            result = permission.confirmation or {
                "success": False,
                "status": "confirmation_required",
            }
        else:
            _, outbound_sensitive = redact_payload(arguments)
            if outbound_sensitive.redactions:
                record_data_flow(
                    source="agent_context",
                    sink=f"mcp:{name}",
                    classification="credential",
                    fields=("tool_arguments",),
                    redactions=outbound_sensitive.redactions,
                    allowed=False,
                    reason="credential-bearing MCP arguments require a dedicated secret binding",
                    conversation_id=conversation_id,
                    task_id=task_id,
                )
                return RuntimeToolOutcome(
                    {
                        "success": False,
                        "status": "error",
                        "error_code": "credential_flow_blocked",
                        "error_message": "MCP 参数包含凭据，已阻止发送；请使用专用密钥绑定",
                    },
                    permission.confirmed,
                    "critical",
                    "mcp",
                )
            record_data_flow(
                source="agent_context",
                sink=f"mcp:{name}",
                classification="internal",
                fields=("tool_arguments",),
                allowed=True,
                reason="approved MCP call",
                conversation_id=conversation_id,
                task_id=task_id,
            )
            try:
                snapshot = create_security_snapshot(
                    workspace,
                    reason=f"before_mcp:{name}",
                    conversation_id=conversation_id,
                    task_id=task_id,
                )
            except SnapshotError as exc:
                return RuntimeToolOutcome(
                    {
                        "success": False,
                        "status": "error",
                        "error_code": "snapshot_failed",
                        "error_message": str(exc),
                    },
                    permission.confirmed,
                    "critical",
                    "mcp",
                )
            data = await invoke_mcp_route(mcp_routes[name], arguments, allow_local_mcp)
            secured, sensitive, findings = secure_untrusted_payload(data, f"mcp:{name}")
            record_data_flow(
                source=f"mcp:{name}",
                sink="agent_context",
                classification=sensitive.classification,
                fields=("mcp_result",),
                redactions=sensitive.redactions,
                allowed=True,
                reason=(
                    f"untrusted MCP result; injection findings: {','.join(findings)}"
                    if findings
                    else "untrusted MCP result"
                ),
                conversation_id=conversation_id,
                task_id=task_id,
            )
            result = {
                "success": True,
                "status": "ok",
                "data": secured,
                "result": secured,
                "security_snapshot_id": snapshot["id"],
            }
        return RuntimeToolOutcome(result, permission.confirmed, "critical", "mcp")

    return await PLUGIN_LIBRARY.execute(call)
