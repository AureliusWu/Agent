from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from .config import settings
from .context import compact_conversation, context_stats, model_history
from .database import audit, connect, now_iso, rows, sanitize_details
from .mcp import discover_mcp_tools, invoke_mcp_route
from .memory import MEMORY_TOOLS, execute_memory_tool, memory_context
from .permissions import authorize
from .provider import completion
from .sandbox import execute_command_async, execute_tool
from .schemas import ChatRequest
from .skills import skill_context
from .task_state import FINAL_TASK_STATUSES, TaskStatus
from .tool_registry import BASE_TOOLS, REGISTRY
from .verification import verify_task


_conversation_locks: dict[int, asyncio.Lock] = {}
_running_tasks: dict[str, asyncio.Task[object]] = {}
_task_slots = asyncio.Semaphore(settings.max_concurrent_tasks)


CompletionCallable = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class TaskLimits:
    max_agent_rounds: int
    task_timeout_seconds: float
    max_task_tokens: int
    max_tool_calls: int
    max_duplicate_tool_calls: int
    max_consecutive_failures: int
    max_no_progress_rounds: int

    @classmethod
    def current(cls) -> "TaskLimits":
        return cls(
            max_agent_rounds=settings.max_agent_rounds,
            task_timeout_seconds=settings.task_timeout_seconds,
            max_task_tokens=settings.max_task_tokens,
            max_tool_calls=settings.max_tool_calls,
            max_duplicate_tool_calls=settings.max_duplicate_tool_calls,
            max_consecutive_failures=settings.max_consecutive_failures,
            max_no_progress_rounds=settings.max_no_progress_rounds,
        )


def _task_update(task_id: str, status: TaskStatus | str, **fields: object) -> None:
    allowed = {
        "termination_reason",
        "model_calls",
        "tool_calls",
        "files_modified",
        "total_tokens",
        "current_step",
        "completed_steps",
        "pending_steps",
        "last_error",
        "started_at",
        "finished_at",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    for key in ("completed_steps", "pending_steps"):
        if key in values:
            values[key] = json.dumps(values[key], ensure_ascii=False)
    normalized_status = TaskStatus(status)
    if normalized_status in FINAL_TASK_STATUSES and "finished_at" not in values:
        values["finished_at"] = now_iso()
    assignments = ["status=?", "updated_at=?", *[f"{key}=?" for key in values]]
    with connect() as db:
        db.execute(
            f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE id=?",
            (normalized_status.value, now_iso(), *values.values(), task_id),
        )


def _record_run(
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
) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO tool_runs(conversation_id, task_id, source, risk, confirmed, tool, status, input, output, started_at, finished_at, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                task_id,
                source,
                risk,
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


def _fingerprint(result: dict[str, Any]) -> str:
    stable = {
        "success": result.get("success"),
        "status": result.get("status"),
        "error_code": result.get("error_code"),
        "data": sanitize_details(result.get("data")),
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stopped_result(task_id: str, status: TaskStatus, reason: str, *, tool_calls: int, files_modified: int) -> dict[str, Any]:
    return {
        "content": f"任务已停止：{reason}。已执行 {tool_calls} 次工具调用，修改文件 {files_modified} 次；未完成步骤没有继续执行。",
        "pending_actions": [],
        "task_id": task_id,
        "task_status": status.value,
    }


def cancel_task(task_id: str) -> dict[str, Any]:
    existing = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not existing:
        raise HTTPException(404, "任务不存在")
    if existing[0]["status"] != TaskStatus.RUNNING.value:
        return {"id": task_id, "status": existing[0]["status"], "interrupted": False}
    task = _running_tasks.get(task_id)
    if task and not task.done():
        task.cancel()
    _task_update(task_id, TaskStatus.CANCELLED, termination_reason="用户主动取消", current_step="cancelled")
    return {"id": task_id, "status": TaskStatus.CANCELLED.value, "interrupted": bool(task)}


def _cancelled_result(task_id: str) -> dict[str, Any]:
    return {
        "content": "任务已取消。已完成的文件操作保留，可在审计中查看并使用撤销工具恢复。",
        "pending_actions": [],
        "task_id": task_id,
        "task_status": TaskStatus.CANCELLED.value,
    }


async def _run_chat(
    payload: ChatRequest,
    api_key: str | None = None,
    *,
    completion_fn: CompletionCallable | None = None,
    limits: TaskLimits | None = None,
) -> dict[str, Any]:
    conversation = rows("SELECT * FROM conversations WHERE id=?", (payload.conversation_id,))
    if not conversation:
        raise HTTPException(404, "对话不存在")
    convo = conversation[0]
    lock = _conversation_locks.setdefault(payload.conversation_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "该对话已有任务正在运行")

    task_id = payload.task_id or uuid.uuid4().hex
    task_started = time.monotonic()
    runtime_limits = limits or TaskLimits.current()
    complete = completion_fn or completion
    existing_tasks = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    resume = bool(payload.approved_actions and existing_tasks and existing_tasks[0]["status"] == TaskStatus.WAITING_CONFIRMATION.value and existing_tasks[0]["conversation_id"] == payload.conversation_id)
    if existing_tasks and not resume:
        raise HTTPException(409, "任务 ID 已存在或不能继续")
    current_task = asyncio.current_task()
    if current_task is not None:
        _running_tasks[task_id] = current_task
    started_at = now_iso()
    with connect() as db:
        if resume:
            db.execute("UPDATE agent_tasks SET status=?, current_step=?, pending_steps='[]', termination_reason=NULL, finished_at=NULL, updated_at=? WHERE id=?", (TaskStatus.RUNNING.value, "resuming", started_at, task_id))
        else:
            db.execute(
                "INSERT INTO agent_tasks(id, conversation_id, status, prompt, current_step, completed_steps, pending_steps, created_at, updated_at, started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, payload.conversation_id, TaskStatus.RUNNING.value, payload.content, "preparing", "[]", "[]", started_at, started_at, started_at),
            )
            db.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
                (payload.conversation_id, "user", payload.content, now_iso()),
            )
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), payload.conversation_id))

    previous_task = existing_tasks[0] if resume else {}
    model_calls = int(previous_task.get("model_calls") or 0)
    tool_call_count = int(previous_task.get("tool_calls") or 0)
    files_modified = int(previous_task.get("files_modified") or 0)
    total_tokens = int(previous_task.get("total_tokens") or 0)
    completed_steps: list[str] = json.loads(previous_task.get("completed_steps") or "[]")
    try:
        async with lock:
            await compact_conversation(payload.conversation_id, api_key)
            servers = rows("SELECT * FROM mcp_servers WHERE enabled=1 ORDER BY name")
            mcp_tools, mcp_routes = await discover_mcp_tools(servers, settings.allow_local_mcp)
            tools = [*BASE_TOOLS, *mcp_tools]
            skill_notes = skill_context(convo["workspace"], payload.content, task_id)
            memory_notes = memory_context(convo["workspace"], payload.content)
            system = (
                f"你是通用 Agent。当前任务 ID 是 {task_id}。工作区是 {convo['workspace']}。权限模式是 {convo['permission_mode']}。"
                "只能使用提供的工具操作工作区；先检查再修改，操作后验证。不能声称执行了未执行的操作。"
                "代码发生变化后，应运行项目已有的测试、构建、类型检查或语法检查；无法验证时必须明确说明。"
                + (f"\n\n{skill_notes}" if skill_notes else "")
                + (f"\n\n{memory_notes}" if memory_notes else "")
            )
            model_messages = [{"role": "system", "content": system}, *model_history(payload.conversation_id)]
            pending: list[dict[str, Any]] = []
            signatures: Counter[str] = Counter()
            consecutive_failures = no_progress_rounds = 0
            previous_round_fingerprint: str | None = None

            for round_number in range(1, runtime_limits.max_agent_rounds + 1):
                if time.monotonic() - task_started > runtime_limits.task_timeout_seconds:
                    reason = f"任务超过 {runtime_limits.task_timeout_seconds} 秒"
                    _task_update(task_id, TaskStatus.TIMED_OUT, termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="timed_out", completed_steps=completed_steps)
                    return _stopped_result(task_id, TaskStatus.TIMED_OUT, reason, tool_calls=tool_call_count, files_modified=files_modified)

                _task_update(task_id, TaskStatus.RUNNING, current_step=f"model_round_{round_number}", model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, completed_steps=completed_steps)
                model_calls += 1
                remaining_seconds = max(runtime_limits.task_timeout_seconds - (time.monotonic() - task_started), 0.001)
                try:
                    message = await asyncio.wait_for(
                        complete(model_messages, api_key, tools=tools, conversation_id=payload.conversation_id, task_id=task_id),
                        timeout=remaining_seconds,
                    )
                except TimeoutError:
                    reason = f"任务超过 {runtime_limits.task_timeout_seconds} 秒"
                    _task_update(task_id, TaskStatus.TIMED_OUT, termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="timed_out", completed_steps=completed_steps)
                    return _stopped_result(task_id, TaskStatus.TIMED_OUT, reason, tool_calls=tool_call_count, files_modified=files_modified)
                metrics = message.pop("_metrics", {})
                total_tokens += int((metrics.get("usage") or {}).get("total_tokens") or 0)
                completed_steps.append(f"model_round_{round_number}")
                if total_tokens > runtime_limits.max_task_tokens:
                    reason = f"任务 Token 用量超过 {runtime_limits.max_task_tokens}"
                    _task_update(task_id, TaskStatus.PARTIALLY_COMPLETED, termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="token_limit", completed_steps=completed_steps)
                    return _stopped_result(task_id, TaskStatus.PARTIALLY_COMPLETED, reason, tool_calls=tool_call_count, files_modified=files_modified)

                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    content = message.get("content") or ""
                    completed_steps.append("final_response")
                    report = verify_task(task_id, convo["workspace"], payload.content, content)
                    verified = report["status"] == "passed"
                    final_status = TaskStatus.COMPLETED if verified else TaskStatus.PARTIALLY_COMPLETED
                    termination_reason = report["summary"]
                    with connect() as db:
                        db.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)", (payload.conversation_id, "assistant", content, now_iso()))
                    _task_update(task_id, final_status, termination_reason=termination_reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="completed" if verified else "verification_incomplete", completed_steps=completed_steps, pending_steps=[])
                    return {"content": content, "pending_actions": pending, "context": context_stats(payload.conversation_id), "task_id": task_id, "task_status": final_status.value, "verification": report}

                model_messages.append(message)
                round_results: list[str] = []
                for call in tool_calls:
                    if tool_call_count >= runtime_limits.max_tool_calls:
                        reason = f"工具调用达到上限 {runtime_limits.max_tool_calls}"
                        _task_update(task_id, TaskStatus.PARTIALLY_COMPLETED, termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="tool_limit", completed_steps=completed_steps)
                        return _stopped_result(task_id, TaskStatus.PARTIALLY_COMPLETED, reason, tool_calls=tool_call_count, files_modified=files_modified)

                    function = call["function"]
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {"_invalid_json": function.get("arguments")}
                    name = function["name"]
                    started, started_perf = now_iso(), time.perf_counter()
                    tool_call_count += 1
                    signature = f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
                    signatures[signature] += 1
                    if signatures[signature] >= runtime_limits.max_duplicate_tool_calls:
                        reason = f"检测到重复工具调用：{name}"
                        _task_update(task_id, TaskStatus.PARTIALLY_COMPLETED, termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="duplicate_call", completed_steps=completed_steps, last_error=reason)
                        return _stopped_result(task_id, TaskStatus.PARTIALLY_COMPLETED, reason, tool_calls=tool_call_count - 1, files_modified=files_modified)

                    if name in mcp_routes:
                        permission = authorize(mode=convo["permission_mode"], risk="critical", tool=name, arguments=arguments, conversation_id=payload.conversation_id, task_id=task_id, approval_tokens=payload.approved_actions, approval_scope=payload.approval_scope, source="mcp", impact="外部 MCP 服务")
                        confirmed = permission.confirmed
                        if not permission.allowed:
                            result = permission.confirmation or {"success": False, "status": "confirmation_required"}
                        else:
                            data = await invoke_mcp_route(mcp_routes[name], arguments, settings.allow_local_mcp)
                            result = {"success": True, "status": "ok", "data": data, "result": data}
                        risk, source = "critical", "mcp"
                    elif name in MEMORY_TOOLS:
                        spec = REGISTRY[name]
                        permission = authorize(mode=convo["permission_mode"], risk=spec.risk, tool=name, arguments=arguments, conversation_id=payload.conversation_id, task_id=task_id, approval_tokens=payload.approved_actions, approval_scope=payload.approval_scope, impact="当前工作区长期记忆")
                        confirmed = permission.confirmed
                        result = execute_memory_tool(convo["workspace"], name, arguments, task_id) if permission.allowed else (permission.confirmation or {"success": False, "status": "confirmation_required"})
                        risk, source = spec.risk, "builtin"
                    else:
                        result = await execute_command_async(convo["workspace"], convo["permission_mode"], arguments, payload.approved_actions, approval_scope=payload.approval_scope, conversation_id=payload.conversation_id, task_id=task_id) if name == "run_command" else execute_tool(convo["workspace"], convo["permission_mode"], name, arguments, payload.approved_actions, approval_scope=payload.approval_scope, conversation_id=payload.conversation_id, task_id=task_id, tool_call_id=call["id"])
                        confirmed = bool(payload.approved_actions and result.get("status") != "confirmation_required")
                        risk, source = REGISTRY.get(name).risk if REGISTRY.get(name) else "critical", "builtin"

                    _record_run(payload.conversation_id, task_id, name, arguments, result, started, started_perf, risk, confirmed, source)
                    completed_steps.append(f"tool:{name}")
                    round_results.append(_fingerprint(result))
                    if result.get("success") and name in {"create_file", "write_file", "replace_text", "apply_patch", "copy_file", "move_file", "rename_file", "create_directory", "delete_file", "undo_file_change", "undo_task_changes"}:
                        files_modified += 1
                    consecutive_failures = 0 if result.get("success") or result.get("status") == "confirmation_required" else consecutive_failures + 1
                    audit(payload.conversation_id, name, str(arguments.get("path") or arguments.get("source") or arguments.get("command") or ""), result.get("status", "ok"), arguments)
                    if result.get("status") == "confirmation_required":
                        pending.append(result)
                    model_messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})

                if pending:
                    pending_steps = [f"approval:{item['tool']}" for item in pending]
                    _task_update(task_id, TaskStatus.WAITING_CONFIRMATION, termination_reason="等待用户确认", model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="waiting_confirmation", completed_steps=completed_steps, pending_steps=pending_steps)
                    return {"content": "以下操作需要你的确认。", "pending_actions": pending, "context": context_stats(payload.conversation_id), "task_id": task_id, "task_status": TaskStatus.WAITING_CONFIRMATION.value}
                if consecutive_failures >= runtime_limits.max_consecutive_failures:
                    reason = f"工具连续失败 {runtime_limits.max_consecutive_failures} 次"
                    _task_update(task_id, TaskStatus.PARTIALLY_COMPLETED, termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="consecutive_failures", completed_steps=completed_steps, last_error=reason)
                    return _stopped_result(task_id, TaskStatus.PARTIALLY_COMPLETED, reason, tool_calls=tool_call_count, files_modified=files_modified)

                round_fingerprint = hashlib.sha256("|".join(round_results).encode("ascii")).hexdigest()
                no_progress_rounds = no_progress_rounds + 1 if round_fingerprint == previous_round_fingerprint else 0
                previous_round_fingerprint = round_fingerprint
                if no_progress_rounds >= runtime_limits.max_no_progress_rounds:
                    reason = f"连续 {runtime_limits.max_no_progress_rounds} 轮没有有效进展"
                    _task_update(task_id, TaskStatus.PARTIALLY_COMPLETED, termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="no_progress", completed_steps=completed_steps, last_error=reason)
                    return _stopped_result(task_id, TaskStatus.PARTIALLY_COMPLETED, reason, tool_calls=tool_call_count, files_modified=files_modified)

            reason = f"Agent 循环达到上限 {runtime_limits.max_agent_rounds} 轮"
            _task_update(task_id, TaskStatus.PARTIALLY_COMPLETED, termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="round_limit", completed_steps=completed_steps)
            return _stopped_result(task_id, TaskStatus.PARTIALLY_COMPLETED, reason, tool_calls=tool_call_count, files_modified=files_modified)
    except asyncio.CancelledError:
        _task_update(task_id, TaskStatus.CANCELLED, termination_reason="用户主动取消", model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="cancelled", completed_steps=completed_steps)
        audit(payload.conversation_id, "chat_cancel", "model", "cancelled", {"task_id": task_id})
        return _cancelled_result(task_id)
    except HTTPException:
        raise
    except Exception as exc:
        _task_update(task_id, TaskStatus.FAILED, termination_reason="执行异常", last_error=str(exc), model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, total_tokens=total_tokens, current_step="failed", completed_steps=completed_steps)
        audit(payload.conversation_id, "chat", "model", "error", {"error": str(exc)})
        raise HTTPException(502, str(exc)) from exc
    finally:
        _running_tasks.pop(task_id, None)


async def run_chat(
    payload: ChatRequest,
    api_key: str | None = None,
    *,
    completion_fn: CompletionCallable | None = None,
    limits: TaskLimits | None = None,
) -> dict[str, Any]:
    try:
        await asyncio.wait_for(_task_slots.acquire(), timeout=settings.task_queue_timeout_seconds)
    except TimeoutError as exc:
        raise HTTPException(503, "任务队列繁忙，请稍后重试") from exc
    try:
        return await _run_chat(payload, api_key, completion_fn=completion_fn, limits=limits)
    finally:
        _task_slots.release()
