from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.database import rows
from app.kernel.errors import KernelContractError
from app.hooks import HookEvent, run_hooks
from app.permissions import PermissionDecision, authorize, expire_task_capabilities
from app.tools.runtime_tools import RuntimeToolOutcome, execute_runtime_tool
from app.sandbox import workspace_root
from app.schemas import ChatRequest
from app.workspace.snapshots import create_security_snapshot
from app.tools.registry import REGISTRY
from app.plugins.registry import PLUGIN_LIBRARY
from app.tools.receipts import build_tool_receipt


@dataclass(frozen=True)
class CapabilitySet:
    platform: str
    tools: tuple[str, ...]
    features: tuple[str, ...]
    workspace_scoped: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "tools": list(self.tools),
            "features": list(self.features),
            "workspace_scoped": self.workspace_scoped,
        }


@dataclass(frozen=True)
class ExecutionContext:
    task_id: str
    workspace: Path
    prepared_at: str
    capabilities: CapabilitySet


@dataclass(frozen=True)
class ExecutorToolCall:
    workspace: str
    mode: str
    name: str
    arguments: dict[str, Any]
    tool_call_id: str
    approved_actions: list[str]
    approval_scope: str
    conversation_id: int
    task_id: str
    mcp_routes: dict[str, Any]
    extension_routes: dict[str, Any] = field(default_factory=dict)
    allow_local_mcp: bool = False
    repair_attempt: int = 0
    retry_scope: list[str] = field(default_factory=list)
    memory_write_policy: str = "explicit"
    memory_write_explicit: bool = False
    search_credentials: dict[str, str] = field(default_factory=dict)
    permission_fn: Callable[..., PermissionDecision] = authorize
    available_tool_names: tuple[str, ...] | None = None

    @classmethod
    def from_runtime_kwargs(cls, values: dict[str, Any]) -> "ExecutorToolCall":
        return cls(
            workspace=str(values["workspace"]),
            mode=str(values["mode"]),
            name=str(values["name"]),
            arguments=dict(values.get("arguments") or {}),
            tool_call_id=str(values["tool_call_id"]),
            approved_actions=list(values.get("approved_actions") or []),
            approval_scope=str(values.get("approval_scope") or "once"),
            conversation_id=int(values["conversation_id"]),
            task_id=str(values["task_id"]),
            mcp_routes=dict(values.get("mcp_routes") or {}),
            extension_routes=dict(values.get("extension_routes") or {}),
            allow_local_mcp=bool(values.get("allow_local_mcp")),
            repair_attempt=int(values.get("repair_attempt") or 0),
            retry_scope=list(values.get("retry_scope") or []),
            memory_write_policy=str(values.get("memory_write_policy") or "explicit"),
            memory_write_explicit=bool(values.get("memory_write_explicit")),
            search_credentials=dict(values.get("search_credentials") or {}),
            permission_fn=values.get("permission_fn") or authorize,
            available_tool_names=tuple(values["available_tool_names"]) if values.get("available_tool_names") is not None else None,
        )


class LocalWindowsExecutor:
    async def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            platform="windows",
            tools=tuple(sorted(name for name in REGISTRY if PLUGIN_LIBRARY.configured(name))),
            features=("files", "commands", "snapshots", "mcp", "cancel", "resume", "plugins"),
        )

    async def prepare(self, task_contract: Any) -> ExecutionContext:
        payload = task_contract if isinstance(task_contract, dict) else vars(task_contract)
        task_id = str(payload.get("task_id") or payload.get("id") or "")
        workspace = str(payload.get("workspace") or "")
        if not task_id or not workspace:
            raise KernelContractError("Executor preparation requires task_id and workspace", component="executor")
        return ExecutionContext(
            task_id=task_id,
            workspace=workspace_root(workspace),
            prepared_at=datetime.now(timezone.utc).isoformat(),
            capabilities=await self.capabilities(),
        )

    async def execute_tool(self, call: ExecutorToolCall) -> RuntimeToolOutcome:
        workspace_root(call.workspace)
        if call.available_tool_names is not None and call.name not in call.available_tool_names:
            denied = {"success": False, "status": "error", "error_code": "tool_scope_violation", "error_message": "工具不在当前任务允许的能力范围内"}
            receipt = build_tool_receipt(call.name, denied)
            denied["receipt"] = receipt.as_dict()
            return RuntimeToolOutcome(
                denied, False, "critical", "executor", receipt,
            )
        await run_hooks(
            HookEvent(
                point="pre_tool",
                conversation_id=call.conversation_id,
                task_id=call.task_id,
                payload={"tool": call.name, "arguments": call.arguments, "tool_call_id": call.tool_call_id},
            )
        )
        outcome = await execute_runtime_tool(
            workspace=call.workspace,
            mode=call.mode,
            name=call.name,
            arguments=call.arguments,
            tool_call_id=call.tool_call_id,
            approved_actions=call.approved_actions,
            approval_scope=call.approval_scope,
            conversation_id=call.conversation_id,
            task_id=call.task_id,
            mcp_routes=call.mcp_routes,
            extension_routes=call.extension_routes,
            allow_local_mcp=call.allow_local_mcp,
            repair_attempt=call.repair_attempt,
            retry_scope=call.retry_scope,
            memory_write_policy=call.memory_write_policy,
            memory_write_explicit=call.memory_write_explicit,
            search_credentials=call.search_credentials,
            permission_fn=call.permission_fn,
            available_tool_names=call.available_tool_names,
        )
        plugin = PLUGIN_LIBRARY.owner(call.name)
        if plugin is not None:
            outcome.result.setdefault("plugin", {"id": plugin.id, "version": plugin.version})
        receipt = build_tool_receipt(call.name, outcome.result)
        outcome.result.setdefault("receipt", receipt.as_dict())
        completed = replace(outcome, receipt=receipt)
        await run_hooks(
            HookEvent(
                point="post_tool",
                conversation_id=call.conversation_id,
                task_id=call.task_id,
                payload={
                    "tool": call.name,
                    "tool_call_id": call.tool_call_id,
                    "success": bool(outcome.result.get("success")),
                    "status": outcome.result.get("status"),
                    "receipt": receipt.as_dict(),
                },
            )
        )
        return completed

    async def snapshot(self, context: ExecutionContext, reason: str = "executor_snapshot") -> dict[str, Any]:
        return create_security_snapshot(str(context.workspace), reason=reason, task_id=context.task_id)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        from app.runtime.runner import cancel_task

        return cancel_task(task_id)

    async def resume(self, task_id: str) -> dict[str, Any]:
        from app.runtime.task_runtime import resume_background_task

        records = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        if not records:
            raise KernelContractError("Executor task does not exist", component="executor", details={"task_id": task_id})
        task = records[0]
        return await resume_background_task(
            ChatRequest(
                conversation_id=int(task["conversation_id"]),
                content=str(task["prompt"]),
                task_id=task_id,
                resume=True,
                orchestration_mode=str(task.get("orchestration_mode") or "single"),
                agent_count=max(1, int(task.get("child_agent_count") or 1)),
            ),
            None,
        )

    async def cleanup(self, task_id: str) -> dict[str, Any]:
        expire_task_capabilities(task_id)
        return {"task_id": task_id, "status": "cleaned"}
