from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.config import settings
from app.database import audit
from app.mcp.rpc import McpError
from app.mcp.tool_adapter import typed_tool_failure
from app.tools.mcp import invoke_mcp_route
from app.workspace.lsp import query_lsp
from app.data_flow import record_data_flow
from app.extensions.sdk import ExtensionToolRoute, RISK_ORDER
from app.memory.service import MEMORY_TOOLS, execute_memory_tool
from app.permissions import PermissionDecision, authorize, permission_for_tool
from app.runtime.repair import repair_tool_allowed
from app.sandbox import execute_command_async, execute_tool
from app.workspace.snapshots import SnapshotError, create_operation_checkpoint
from app.tools.registry import REGISTRY, ToolValidationError, validate_arguments
from app.tools.receipts import ToolReceipt
from app.tools.file_operations import CORE_FILE_OPERATIONS, FileOperationRequest, execute_file_batch, execute_file_operation
from app.tools.delegation_grants import issue_delegate_permission
from app.runtime.task_events import emit_task_event
from app.security.trust import redact_payload, secure_untrusted_payload
from app.security.local_only import local_only_policy, mcp_route_is_external
from app.providers.web_search import fetch_web_page, search_web


VISION_TOOLS = {
    "vision.describe": "describe",
    "vision.extract_text": "extract_text",
    "vision.analyze_chart": "analyze_chart",
    "vision.compare": "compare",
    "vision.inspect_ui": "inspect_ui",
    "vision.classify": "classify",
}


@dataclass(frozen=True)
class RuntimeToolOutcome:
    result: dict[str, Any]
    confirmed: bool
    risk: str
    source: str
    receipt: ToolReceipt | None = None


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
) -> RuntimeToolOutcome:
    extension_route = (extension_routes or {}).get(name)
    canonical_name = extension_route.delegate if extension_route else name
    if local_only_policy().enabled and (
        permission_for_tool(canonical_name) == "network.request"
        or (name in mcp_routes and mcp_route_is_external(mcp_routes[name]))
    ):
        risk = REGISTRY.get(canonical_name).risk if REGISTRY.get(canonical_name) else "critical"
        return RuntimeToolOutcome(
            {
                "success": False,
                "status": "error",
                "error_code": "offline_network_blocked",
                "error_message": "当前使用 Ollama 本地离线模式，已阻止外部网络工具",
            },
            False,
            risk,
            "local_only_policy",
        )
    memory_mutation_blocked = canonical_name in {"remember_workspace", "forget_workspace_memory"} and (
        memory_write_policy == "deny" or (memory_write_policy == "explicit" and not memory_write_explicit)
    )
    memory_policy_result = {
        "success": False,
        "status": "error",
        "error_code": "memory_write_policy_denied",
        "error_message": "当前任务的记忆写入策略不允许这次变更",
    }
    if repair_attempt and not repair_tool_allowed(canonical_name, retry_scope or []):
        result = {
            "success": False,
            "status": "error",
            "error_code": "repair_scope_violation",
            "error": f"限定返工不允许重复执行已通过的 {name} 步骤",
        }
        risk = REGISTRY.get(canonical_name).risk if REGISTRY.get(canonical_name) else "critical"
        return RuntimeToolOutcome(result, False, risk, "verifier")

    if extension_route is not None:
        try:
            merged_arguments = extension_route.resolve_arguments(arguments)
            delegate_spec = validate_arguments(extension_route.delegate, merged_arguments)
            if (delegate_spec.risk == "critical"
                    or RISK_ORDER.get(extension_route.risk, -1) < RISK_ORDER[delegate_spec.risk]):
                raise ValueError("扩展不得代理critical工具或降低底层工具风险")
        except (ToolValidationError, ValueError) as exc:
            return RuntimeToolOutcome(
                {"success": False, "status": "error", "error_code": "invalid_extension_arguments", "error_message": str(exc)},
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
            impact=str(merged_arguments.get("path") or merged_arguments.get("source") or "当前工作区"),
            workspace=workspace,
        )
        if not permission.allowed:
            result = permission.confirmation or {"success": False, "status": "confirmation_required"}
        else:
            delegated_permission = issue_delegate_permission(
                decision=permission, workspace=workspace, mode=mode, conversation_id=conversation_id,
                task_id=task_id, parent_tool=name, parent_source=f"extension:{extension_route.extension_id}",
                parent_risk=extension_route.risk, delegate=extension_route.delegate, arguments=merged_arguments,
            )
            if extension_route.delegate in MEMORY_TOOLS:
                delegated = delegated_permission(mode=mode, workspace=workspace, conversation_id=conversation_id,
                    task_id=task_id, tool=extension_route.delegate, risk=delegate_spec.risk, arguments=merged_arguments)
                result = ((memory_policy_result if memory_mutation_blocked else execute_memory_tool(
                    workspace, extension_route.delegate, merged_arguments, task_id))
                    if delegated.allowed else delegated.confirmation)
            else:
                result = execute_tool(
                    workspace, mode, extension_route.delegate, merged_arguments, [], approval_scope="once",
                    conversation_id=conversation_id, task_id=task_id, tool_call_id=tool_call_id,
                    permission_fn=delegated_permission,
                )
        return RuntimeToolOutcome(result, permission.confirmed, extension_route.risk, f"extension:{extension_route.extension_id}")

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
            result = permission.confirmation or {"success": False, "status": "confirmation_required"}
        else:
            _, outbound_sensitive = redact_payload(arguments)
            if outbound_sensitive.redactions:
                record_data_flow(source="agent_context", sink=f"mcp:{name}", classification="credential", fields=("tool_arguments",), redactions=outbound_sensitive.redactions, allowed=False, reason="credential-bearing MCP arguments require a dedicated secret binding", conversation_id=conversation_id, task_id=task_id)
                return RuntimeToolOutcome({"success": False, "status": "error", "error_code": "credential_flow_blocked", "error_message": "MCP 参数包含凭据，已阻止发送；请使用专用密钥绑定"}, permission.confirmed, "critical", "mcp")
            record_data_flow(source="agent_context", sink=f"mcp:{name}", classification="internal", fields=("tool_arguments",), allowed=True, reason="approved MCP call", conversation_id=conversation_id, task_id=task_id)
            try:
                snapshot = create_operation_checkpoint(
                    workspace,
                    reason=f"before_mcp:{name}",
                    operation_scope="external_mcp",
                    affected_paths=[],
                    conversation_id=conversation_id,
                    task_id=task_id,
                )
            except SnapshotError as exc:
                return RuntimeToolOutcome({"success": False, "status": "error", "error_code": "snapshot_failed", "error_message": str(exc)}, permission.confirmed, "critical", "mcp")
            try:
                data = await invoke_mcp_route(mcp_routes[name], arguments, allow_local_mcp)
            except McpError as exc:
                result = typed_tool_failure(exc, snapshot_id=str(snapshot["id"]))
                result.update(
                    operation_scope=snapshot["operation_scope"],
                    rollback_scope=snapshot["rollback_scope"],
                    rollback_paths=snapshot["rollback_paths"],
                )
                audit(
                    conversation_id,
                    "mcp_call",
                    name,
                    "error",
                    {
                        "error_code": result["error_code"],
                        "rpc_error_code": result.get("rpc_error_code"),
                        "retryable": result["retryable"],
                    },
                )
                return RuntimeToolOutcome(result, permission.confirmed, "critical", "mcp")
            secured, sensitive, findings = secure_untrusted_payload(data, f"mcp:{name}")
            record_data_flow(
                source=f"mcp:{name}",
                sink="agent_context",
                classification=sensitive.classification,
                fields=("mcp_result",),
                redactions=sensitive.redactions,
                allowed=True,
                reason=f"untrusted MCP result; injection findings: {','.join(findings)}" if findings else "untrusted MCP result",
                conversation_id=conversation_id,
                task_id=task_id,
            )
            result = {
                "success": True,
                "status": "ok",
                "data": secured,
                "result": secured,
                "security_snapshot_id": snapshot["id"],
                "operation_scope": snapshot["operation_scope"],
                "rollback_scope": snapshot["rollback_scope"],
                "rollback_paths": snapshot["rollback_paths"],
            }
        return RuntimeToolOutcome(result, permission.confirmed, "critical", "mcp")

    if name in MEMORY_TOOLS:
        spec = REGISTRY[name]
        permission = permission_fn(
            mode=mode,
            risk=spec.risk,
            tool=name,
            arguments=arguments,
            conversation_id=conversation_id,
            task_id=task_id,
            approval_tokens=approved_actions,
            approval_scope=approval_scope,
            impact="当前工作区长期记忆",
            workspace=workspace,
        )
        result = (
            (memory_policy_result if memory_mutation_blocked else execute_memory_tool(workspace, name, arguments, task_id))
            if permission.allowed
            else (permission.confirmation or {"success": False, "status": "confirmation_required"})
        )
        return RuntimeToolOutcome(result, permission.confirmed, spec.risk, "builtin")

    if name in CORE_FILE_OPERATIONS:
        adapter = CORE_FILE_OPERATIONS[name]
        result = execute_file_operation(
            workspace,
            FileOperationRequest(name, arguments),
            mode=mode,
            approval_tokens=approved_actions,
            approval_scope=approval_scope,
            conversation_id=conversation_id,
            task_id=task_id,
            tool_call_id=tool_call_id,
            permission_fn=permission_fn,
        )
        confirmed = bool(approved_actions and result.get("status") != "confirmation_required")
        return RuntimeToolOutcome(result, confirmed, REGISTRY[adapter].risk, "builtin:file_core")

    if name == "file_batch":
        spec = REGISTRY[name]
        try:
            validate_arguments(name, arguments)
        except ToolValidationError as exc:
            return RuntimeToolOutcome(
                {"success": False, "status": "error", "error_code": "invalid_arguments", "error_message": str(exc)},
                False,
                spec.risk,
                "builtin:file_transaction",
            )
        requests = [
            FileOperationRequest(str(item.get("operation") or ""), dict(item.get("arguments") or {}))
            for item in arguments.get("operations", [])
            if isinstance(item, dict)
        ]
        result = execute_file_batch(
            workspace,
            requests,
            mode=mode,
            dry_run=bool(arguments.get("dry_run", False)),
            approval_tokens=approved_actions,
            approval_scope=approval_scope,
            conversation_id=conversation_id,
            task_id=task_id,
            permission_fn=permission_fn,
        )
        return RuntimeToolOutcome(result, bool(result.get("confirmed")), spec.risk, "builtin:file_transaction")

    if name.startswith("artifact."):
        # Keep OOXML/PDF render dependencies off normal startup and text-only paths.
        from app.artifacts.service import ARTIFACT_TOOLS, execute_artifact_tool

        if name in ARTIFACT_TOOLS:
            spec = REGISTRY[name]
            try:
                validate_arguments(name, arguments)
            except ToolValidationError as exc:
                return RuntimeToolOutcome(
                    {
                        "success": False,
                        "status": "error",
                        "error_code": "invalid_arguments",
                        "error_message": str(exc),
                    },
                    False,
                    spec.risk,
                    "builtin:artifact",
                )
            permission = permission_fn(
                mode=mode,
                risk=spec.risk,
                tool=name,
                arguments=arguments,
                conversation_id=conversation_id,
                task_id=task_id,
                approval_tokens=approved_actions,
                approval_scope=approval_scope,
                impact=str(
                    arguments.get("path")
                    or arguments.get("output_directory")
                    or "current workspace artifact"
                ),
                workspace=workspace,
            )
            result = (
                execute_artifact_tool(
                    workspace,
                    name,
                    arguments,
                    task_id=task_id,
                    tool_call_id=tool_call_id,
                )
                if permission.allowed
                else (
                    permission.confirmation
                    or {"success": False, "status": "confirmation_required"}
                )
            )
            return RuntimeToolOutcome(
                result,
                permission.confirmed,
                spec.risk,
                "builtin:artifact",
            )

    if name in VISION_TOOLS:
        # Keep the image stack off the normal text-task and startup paths.
        from app.vision import VisionError, VisionService

        spec = REGISTRY[name]
        permission = permission_fn(
            mode=mode,
            risk=spec.risk,
            tool=name,
            arguments=arguments,
            conversation_id=conversation_id,
            task_id=task_id,
            approval_tokens=approved_actions,
            approval_scope=approval_scope,
            impact=str(arguments.get("path") or "工作区图片"),
            workspace=workspace,
        )
        if not permission.allowed:
            result = permission.confirmation or {"success": False, "status": "confirmation_required"}
        else:
            paths = arguments.get("paths")
            if not isinstance(paths, list):
                paths = [str(arguments.get("path") or "")]
            try:
                result = await VisionService().analyze(
                    workspace=workspace,
                    paths=[str(path) for path in paths],
                    action=VISION_TOOLS[name],
                    prompt=str(arguments.get("prompt") or ""),
                    provider_mode="local",
                    conversation_id=conversation_id,
                    task_id=task_id,
                )
            except VisionError as exc:
                result = {
                    "success": False,
                    "status": "error",
                    "error_code": exc.code,
                    "error_message": str(exc),
                }
        return RuntimeToolOutcome(result, permission.confirmed, spec.risk, "builtin:vision")

    if name == "lsp_query":
        spec = REGISTRY[name]
        permission = permission_fn(
            mode=mode,
            risk=spec.risk,
            tool=name,
            arguments=arguments,
            conversation_id=conversation_id,
            task_id=task_id,
            approval_tokens=approved_actions,
            approval_scope=approval_scope,
            impact=str(arguments.get("path") or "current workspace"),
            workspace=workspace,
        )
        if not permission.allowed:
            result = permission.confirmation or {"success": False, "status": "confirmation_required"}
        else:
            result = await query_lsp(
                workspace,
                str(arguments["path"]),
                str(arguments["operation"]),
                line=int(arguments.get("line") or 0),
                character=int(arguments.get("character") or 0),
                symbol=str(arguments.get("symbol") or ""),
            )
        return RuntimeToolOutcome(result, permission.confirmed, spec.risk, "builtin:lsp")

    if name == "run_command":
        result = await execute_command_async(
            workspace,
            mode,
            arguments,
            approved_actions,
            approval_scope=approval_scope,
            conversation_id=conversation_id,
            task_id=task_id,
            permission_fn=permission_fn,
        )
    elif name == "web_search":
        spec = REGISTRY[name]
        permission = permission_fn(
            mode=mode,
            risk=spec.risk,
            tool=name,
            arguments=arguments,
            conversation_id=conversation_id,
            task_id=task_id,
            approval_tokens=approved_actions,
            approval_scope=approval_scope,
            impact=f"联网搜索: {str(arguments.get('query', ''))[:80]}",
            workspace=workspace,
        )
        if not permission.allowed:
            result = permission.confirmation or {"success": False, "status": "confirmation_required"}
        else:
            query = str(arguments.get("query", "")).strip()
            if not query:
                result = {"success": False, "status": "error", "error_code": "invalid_arguments", "error_message": "搜索关键词不能为空"}
            else:
                emit_task_event(task_id, "search.started", {"provider": str(arguments.get("provider") or settings.default_search_provider), "query": query})
                result = await search_web(
                    query,
                    provider=str(arguments.get("provider") or settings.default_search_provider),
                    credentials=search_credentials,
                    max_results=int(arguments.get("max_results") or 8),
                    topic=str(arguments.get("topic") or "general"),
                    time_range=str(arguments.get("time_range") or "") or None,
                )
                emit_task_event(
                    task_id,
                    "search.completed",
                    {
                        "provider": result.get("provider") or str(arguments.get("provider") or settings.default_search_provider),
                        "query": query,
                        "duration_ms": result.get("duration_ms"),
                        "result_count": result.get("result_count", 0),
                        "success": bool(result.get("success")),
                    },
                )
        return RuntimeToolOutcome(result, permission.confirmed, spec.risk, "builtin:web_search")
    elif name == "web_fetch":
        spec = REGISTRY[name]
        permission = permission_fn(
            mode=mode, risk=spec.risk, tool=name, arguments=arguments,
            conversation_id=conversation_id, task_id=task_id,
            approval_tokens=approved_actions, approval_scope=approval_scope,
            impact=str(arguments.get("url") or "public web page"), workspace=workspace,
        )
        result = (
            await fetch_web_page(str(arguments.get("url") or ""), max_chars=int(arguments.get("max_chars") or 40_000))
            if permission.allowed
            else (permission.confirmation or {"success": False, "status": "confirmation_required"})
        )
        return RuntimeToolOutcome(result, permission.confirmed, spec.risk, "builtin:web_fetch")
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
            permission_fn=permission_fn,
        )
    confirmed = bool(approved_actions and result.get("status") != "confirmation_required")
    risk = REGISTRY.get(name).risk if REGISTRY.get(name) else "critical"
    return RuntimeToolOutcome(result, confirmed, risk, "builtin")
