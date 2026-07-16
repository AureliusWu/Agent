from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .database import rows
from .kernel.errors import KernelContractError
from .hooks import HookEvent, run_hooks
from .permissions import PermissionDecision, authorize, expire_task_capabilities
from .runtime_tools import RuntimeToolOutcome, execute_runtime_tool
from .sandbox import workspace_root
from .schemas import ChatRequest
from .snapshots import create_security_snapshot
from .tool_registry import REGISTRY
from .tool_receipts import build_tool_receipt


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
    permission_fn: Callable[..., PermissionDecision] = authorize

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
            permission_fn=values.get("permission_fn") or authorize,
        )


class LocalWindowsExecutor:
    async def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            platform="windows",
            tools=tuple(sorted(REGISTRY)),
            features=("files", "commands", "snapshots", "mcp", "pause", "cancel", "resume"),
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
            permission_fn=call.permission_fn,
        )
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

    async def pause(self, task_id: str) -> dict[str, Any]:
        from .task_runner import pause_task

        return await pause_task(task_id)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        from .task_runner import cancel_task

        return cancel_task(task_id)

    async def resume(self, task_id: str) -> dict[str, Any]:
        from .task_runtime import resume_background_task

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
