"""Independent evidence verification, not task-completion authority."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.hooks import HookEvent
from app.kernel.services import KernelServices
from app.runtime.task_state import TaskStatus


async def verify_candidate(
    services: KernelServices,
    *,
    task_id: str,
    conversation_id: int,
    workspace: str,
    plan: Any,
    content: str,
    repair_count: int,
    active_repair_attempt: int,
    active_repair_fingerprint: str | None,
    profile: Any,
    task_fields: dict[str, Any],
    run_hooks: Callable[[HookEvent], Awaitable[Any]],
    finish_repair: Callable[..., Any],
    emit: Callable[..., Any],
) -> dict[str, Any]:
    hooks = await run_hooks(HookEvent(
        point="pre_complete", conversation_id=conversation_id, task_id=task_id,
        payload={"content_length": len(content), "repair_attempts": repair_count},
    ))
    if hooks:
        emit("hook.completed", {"point": "pre_complete", "outcomes": hooks})
    services.tasks.update_task(
        task_id, TaskStatus.VERIFYING, current_step="verifying", current_phase="verification", **task_fields,
    )
    report = services.verifier.verify(
        task_id, workspace, plan, content,
        previous_evidence_fingerprint=active_repair_fingerprint,
        agent_profile_id=profile.id, verifier_id=profile.verifier_id,
        completion_standards=profile.completion_standards,
    )
    emit("verification.completed", {"status": report["status"], "summary": report["summary"]})
    if active_repair_attempt:
        finish_repair(task_id, active_repair_attempt, report)
    return report
