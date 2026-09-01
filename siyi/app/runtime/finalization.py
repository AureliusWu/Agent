"""Response persistence and post-verification effects with explicit dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.hooks import HookEvent
from app.kernel.services import KernelServices
from app.runtime.task_state import RESUMABLE_TASK_STATUSES, TaskStatus


@dataclass(frozen=True)
class FinalizationCallbacks:
    # Runner supplies these at invocation time to preserve its public patch seams.
    extract_candidates: Callable[..., Any]
    record_interaction: Callable[..., Any]
    consolidate: Callable[[], Any]
    schedule_title: Callable[..., Any]
    run_hooks: Callable[[HookEvent], Awaitable[Any]]


def stopped_result(task_id: str, status: TaskStatus, reason: str, *, tool_calls: int, files_modified: int) -> dict[str, Any]:
    return {
        "content": f"任务已停止：{reason}。已执行 {tool_calls} 次工具调用，修改文件 {files_modified} 次；未完成步骤没有继续执行。",
        "pending_actions": [], "task_id": task_id, "task_status": status.value,
        "resumable": status in RESUMABLE_TASK_STATUSES,
    }


def cancelled_result(task_id: str) -> dict[str, Any]:
    return {
        "content": "任务已取消。已完成的文件操作保留，可在审计中查看并使用撤销工具恢复。",
        "pending_actions": [], "task_id": task_id, "task_status": TaskStatus.CANCELLED.value,
    }


def persist_response(
    services: KernelServices,
    callbacks: FinalizationCallbacks,
    *,
    conversation_id: int,
    task_id: str,
    prompt: str,
    content: str,
    reasoning: str,
) -> None:
    services.tasks.append_message(conversation_id, "assistant", content, task_id=task_id, reasoning=reasoning)
    callbacks.extract_candidates(prompt, conversation_id=conversation_id)
    callbacks.record_interaction(conversation_id)
    callbacks.consolidate()


def completed_result(
    services: KernelServices, *, conversation_id: int, task_id: str, content: str,
    reasoning: str, final_status: TaskStatus, report: dict[str, Any], usage: dict[str, Any],
) -> dict[str, Any]:
    return {
        "content": content, "reasoning": reasoning, "pending_actions": [],
        "context": services.context.stats(conversation_id), "task_id": task_id,
        "task_status": final_status.value, "verification": report, "usage": usage, "resumable": False,
    }


async def finalize_workspace_task(
    services: KernelServices,
    callbacks: FinalizationCallbacks,
    *,
    conversation_id: int,
    task_id: str,
    prompt: str,
    content: str,
    reasoning: str,
    api_key: str | None,
    report: dict[str, Any],
    final_fields: dict[str, Any],
    memory_write_policy: str,
    workspace: str,
    retrieved_memory_ids: list[int],
    known_errors: list[dict[str, Any]],
    modified_paths: list[str],
    usage: dict[str, Any],
    emit: Callable[..., Any],
    close_segment: Callable[[str, str], None],
) -> dict[str, Any]:
    persist_response(
        services, callbacks, conversation_id=conversation_id, task_id=task_id,
        prompt=prompt, content=content, reasoning=reasoning,
    )
    # Only the registered Verifier can decide and persist the terminal status.
    final_status = services.verifier.finalize(task_id, report, **final_fields)
    if final_status == TaskStatus.COMPLETED:
        callbacks.schedule_title(conversation_id, prompt, content, api_key)
    services.memory.record_outcome(retrieved_memory_ids, report["status"] == "passed")
    if memory_write_policy == "allow":
        services.memory.capture_experience(workspace, task_id, known_errors, report, modified_paths)
    hooks = await callbacks.run_hooks(HookEvent(
        point="post_complete", conversation_id=conversation_id, task_id=task_id,
        payload={"status": final_status.value, "verification": report["status"]},
    ))
    if hooks:
        emit("hook.completed", {"point": "post_complete", "outcomes": hooks})
    close_segment("completed", final_status.value)
    return completed_result(
        services, conversation_id=conversation_id, task_id=task_id, content=content,
        reasoning=reasoning, final_status=final_status, report=report, usage=usage,
    )
