from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..context import build_current_context, build_working_memory, compact_conversation, context_stats, model_history, render_layered_context
from ..database import audit, connect, now_iso, rows, sanitize_details
from ..data_flow import record_data_flow
from ..extensions_runtime import active_extension_profiles, active_extension_skill_paths, active_extension_tools
from ..memory import capture_task_experience, invalidate_project_signature, record_memory_outcome, retrieve_memories
from ..permissions import authorize, expire_task_capabilities
from ..executor import ExecutorToolCall
from ..sandbox import recover_file_operation, verify_task_changes, workspace_root
from ..skills import skill_context
from ..task_state import FINAL_TASK_STATUSES, RESUMABLE_TASK_STATUSES, TaskStatus
from ..task_events import emit_task_event
from ..verification import finalize_task_from_verification, verify_task
from .errors import KernelContractError


CompletionCallable = Callable[..., Awaitable[dict[str, Any]]]
ToolCallable = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class OpenAICompatibleProviderAdapter:
    completion_fn: CompletionCallable

    async def complete(self, messages: list[dict[str, Any]], api_key: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self.completion_fn(messages, api_key, **kwargs)

    def capabilities(self) -> list[dict[str, Any]]:
        from ..provider_capabilities import configured_provider_matrix

        return configured_provider_matrix()

    async def probe(self, api_key: str | None = None) -> dict[str, Any]:
        from ..provider import provider_health

        return await provider_health(api_key)


CallableModelProvider = OpenAICompatibleProviderAdapter


@dataclass(frozen=True)
class RuntimeToolProvider:
    executor: Any
    execute_fn: ToolCallable | None = None
    permission_policy: Any = None

    async def execute(self, **kwargs: Any) -> Any:
        if self.permission_policy is not None:
            kwargs.setdefault("permission_fn", self.permission_policy.authorize)
        if self.execute_fn is not None:
            return await self.execute_fn(**kwargs)
        return await self.executor.execute_tool(ExecutorToolCall.from_runtime_kwargs(kwargs))


class DefaultPermissionPolicy:
    def authorize(self, **kwargs: Any) -> Any:
        return authorize(**kwargs)

    def expire_task(self, task_id: str) -> None:
        expire_task_capabilities(task_id)


class DefaultContextProvider:
    def history(self, conversation_id: int) -> list[dict[str, str]]:
        return model_history(conversation_id)

    def stats(self, conversation_id: int) -> dict[str, Any]:
        return context_stats(conversation_id)

    def current(self, **kwargs: Any) -> dict[str, Any]:
        return build_current_context(**kwargs)

    def working(self, **kwargs: Any) -> dict[str, Any]:
        return build_working_memory(**kwargs)

    def render(self, current: dict[str, Any], working: dict[str, Any]) -> str:
        return render_layered_context(current, working)

    def skills(self, workspace: str, prompt: str, task_id: str | None = None) -> str:
        return skill_context(workspace, prompt, task_id)

    async def compact(self, conversation_id: int, api_key: str | None, force: bool = False, *, task_id: str | None = None) -> dict[str, Any]:
        return await compact_conversation(conversation_id, api_key, force, task_id=task_id)


class DefaultMemoryProvider:
    def retrieve(self, workspace: str, prompt: str) -> dict[str, Any]:
        return retrieve_memories(workspace, prompt)

    def record_outcome(self, memory_ids: list[int], success: bool) -> None:
        record_memory_outcome(memory_ids, success)

    def capture_experience(self, workspace: str, task_id: str, errors: list[dict[str, Any]], report: dict[str, Any], files: list[str]) -> None:
        capture_task_experience(workspace, task_id, errors, report, files)

    def invalidate_project(self, workspace: str) -> None:
        invalidate_project_signature(workspace)


class DefaultWorkspaceProvider:
    def root(self, workspace: str) -> Path:
        return workspace_root(workspace)

    def recover_operation(self, workspace: str, task_id: str, tool_call_id: str) -> dict[str, Any] | None:
        return recover_file_operation(workspace, task_id, tool_call_id)

    def verify_changes(self, workspace: str, task_id: str) -> dict[str, Any]:
        return verify_task_changes(workspace, task_id)


TASK_UPDATE_FIELDS = {
    "termination_reason",
    "model_calls",
    "tool_calls",
    "files_modified",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "phase_tokens",
    "estimated_cost_usd",
    "model_route",
    "cache_hits",
    "cache_misses",
    "current_step",
    "completed_steps",
    "pending_steps",
    "last_error",
    "started_at",
    "finished_at",
    "repair_attempts",
    "verification_attempts",
    "current_phase",
    "checkpoint_sequence",
    "resume_count",
    "resumable",
    "paused_at",
    "orchestration_mode",
    "child_agent_count",
}


class SqliteTaskStore:
    def conversation(self, conversation_id: int) -> dict[str, Any] | None:
        records = rows("SELECT * FROM conversations WHERE id=?", (conversation_id,))
        return records[0] if records else None

    def task(self, task_id: str) -> dict[str, Any] | None:
        records = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        return records[0] if records else None

    def start_task(
        self,
        *,
        task_id: str,
        conversation_id: int,
        prompt: str,
        orchestration_mode: str,
        agent_profile_id: str,
        agent_profile_snapshot: dict[str, Any],
        current_phase: str,
        current_step: str,
        started_at: str,
    ) -> None:
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id, conversation_id, status, prompt, orchestration_mode, agent_profile_id, agent_profile_snapshot, current_phase, current_step, completed_steps, pending_steps, created_at, updated_at, started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    conversation_id,
                    TaskStatus.RUNNING.value,
                    prompt,
                    orchestration_mode,
                    agent_profile_id,
                    json.dumps(agent_profile_snapshot, ensure_ascii=False),
                    current_phase,
                    current_step,
                    "[]",
                    "[]",
                    started_at,
                    started_at,
                    started_at,
                ),
            )
            db.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
                (conversation_id, "user", prompt, now_iso()),
            )
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), conversation_id))

    def resume_task(self, task_id: str, updated_at: str) -> None:
        with connect() as db:
            db.execute(
                "UPDATE agent_tasks SET status=?, current_step=?, pending_steps='[]', termination_reason=NULL, finished_at=NULL, "
                "paused_at=NULL, resumable=1, resume_count=resume_count+1, updated_at=? WHERE id=?",
                (TaskStatus.RUNNING.value, "resuming", updated_at, task_id),
            )

    def append_message(self, conversation_id: int, role: str, content: str) -> None:
        with connect() as db:
            db.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
                (conversation_id, role, content, now_iso()),
            )

    def latest_running_repair(self, task_id: str) -> dict[str, Any] | None:
        records = rows(
            "SELECT * FROM task_repair_runs WHERE task_id=? AND status='running' ORDER BY attempt DESC LIMIT 1",
            (task_id,),
        )
        return records[0] if records else None

    def update_task(self, task_id: str, status: Any, **fields: object) -> None:
        values = {key: value for key, value in fields.items() if key in TASK_UPDATE_FIELDS}
        for key in ("completed_steps", "pending_steps", "phase_tokens", "model_route"):
            if key in values:
                values[key] = json.dumps(values[key], ensure_ascii=False)
        normalized_status = TaskStatus(status)
        if normalized_status == TaskStatus.COMPLETED:
            raise KernelContractError("completed 只能由独立 Verifier 写入", component="task_store")
        if normalized_status in FINAL_TASK_STATUSES and "finished_at" not in values:
            values["finished_at"] = now_iso()
            values.setdefault("resumable", 0)
        elif normalized_status in RESUMABLE_TASK_STATUSES:
            values.setdefault("resumable", 1)
            values.setdefault("finished_at", None)
        assignments = ["status=?", "updated_at=?", *[f"{key}=?" for key in values]]
        with connect() as db:
            db.execute(
                f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE id=?",
                (normalized_status.value, now_iso(), *values.values(), task_id),
            )
        if normalized_status in FINAL_TASK_STATUSES:
            expire_task_capabilities(task_id)

    def record_tool_run(
        self,
        *,
        conversation_id: int,
        task_id: str,
        tool: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        started: str,
        started_perf: float,
        risk: str,
        confirmed: bool,
        source: str = "builtin",
        execution_id: str | None = None,
    ) -> None:
        with connect() as db:
            db.execute(
                "INSERT INTO tool_runs(conversation_id, task_id, source, risk, execution_id, confirmed, tool, status, input, output, started_at, finished_at, duration_ms) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(execution_id) WHERE execution_id IS NOT NULL DO UPDATE SET source=excluded.source, risk=excluded.risk, confirmed=excluded.confirmed, "
                "status=excluded.status, input=excluded.input, output=excluded.output, finished_at=excluded.finished_at, duration_ms=excluded.duration_ms",
                (
                    conversation_id,
                    task_id,
                    source,
                    risk,
                    execution_id,
                    int(confirmed),
                    tool,
                    result.get("status", "ok"),
                    json.dumps(sanitize_details(arguments), ensure_ascii=False),
                    json.dumps(sanitize_details(result), ensure_ascii=False)[:40_000],
                    started,
                    now_iso(),
                    round((time.perf_counter() - started_perf) * 1000),
                ),
            )
        event_type = "tool.completed" if result.get("success") else "tool.failed"
        if result.get("status") == "confirmation_required":
            event_type = "tool.waiting_confirmation"
        emit_task_event(
            task_id,
            event_type,
            {
                "tool": tool,
                "status": result.get("status", "ok"),
                "success": bool(result.get("success")),
                "execution_id": execution_id,
            },
        )


class DefaultEvaluator:
    async def run(self, **kwargs: Any) -> Any:
        from ..evals.runner import run_evaluation

        return await run_evaluation(**kwargs)


class DefaultVerifier:
    def verify(self, task_id: str, workspace: str, plan: Any, response: str, **kwargs: Any) -> dict[str, Any]:
        emit_task_event(task_id, "verification.started", {})
        return verify_task(task_id, workspace, plan, response, **kwargs)

    def finalize(self, task_id: str, report: dict[str, Any], **fields: Any) -> Any:
        return finalize_task_from_verification(task_id, report, **fields)


class DatabaseTraceExporter:
    def audit(self, conversation_id: int | None, action: str, target: str, status: str, details: Any = None) -> None:
        audit(conversation_id, action, target, status, details)

    def data_flow(self, **kwargs: Any) -> None:
        record_data_flow(**kwargs)


class DeclarativeExtensionProvider:
    def active_tools(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return active_extension_tools()

    def active_profiles(self) -> list[dict[str, Any]]:
        return active_extension_profiles()

    def active_skills(self) -> list[dict[str, str]]:
        return active_extension_skill_paths()
