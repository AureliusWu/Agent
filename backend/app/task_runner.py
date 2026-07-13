from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter

from fastapi import HTTPException

from .config import settings
from .context import compact_conversation, context_stats, model_history
from .database import audit, connect, now_iso, rows, sanitize_details
from .mcp import discover_mcp_tools, invoke_mcp_route
from .provider import completion
from .sandbox import approval_key, execute_command_async, execute_tool
from .schemas import ChatRequest
from .skills import skill_context
from .tool_registry import BASE_TOOLS, REGISTRY, requires_confirmation

TASK_TIMEOUT_SECONDS = 300
MAX_AGENT_ROUNDS = 12

_conversation_locks: dict[int, asyncio.Lock] = {}
_running_tasks: dict[str, asyncio.Task[object]] = {}


def _task_update(task_id: str, status: str, **fields: object) -> None:
    allowed = {"termination_reason", "model_calls", "tool_calls", "files_modified", "last_error"}
    values = {key: value for key, value in fields.items() if key in allowed}
    assignments = ["status=?", "updated_at=?", *[f"{key}=?" for key in values]]
    with connect() as db:
        db.execute(
            f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE id=?",
            (status, now_iso(), *values.values(), task_id),
        )


def _record_run(
    conversation_id: int,
    task_id: str,
    tool: str,
    arguments: dict,
    result: dict,
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


def cancel_task(task_id: str) -> dict:
    existing = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not existing:
        raise HTTPException(404, "任务不存在")
    if existing[0]["status"] != "running":
        return {"id": task_id, "status": existing[0]["status"], "interrupted": False}
    task = _running_tasks.get(task_id)
    if task and not task.done():
        task.cancel()
    _task_update(task_id, "cancelled", termination_reason="用户主动取消")
    return {"id": task_id, "status": "cancelled", "interrupted": bool(task)}


def _cancelled_result(task_id: str) -> dict:
    return {
        "content": "任务已取消。已完成的文件操作保留，可在审计中查看并使用撤销工具恢复。",
        "pending_actions": [],
        "task_id": task_id,
        "task_status": "cancelled",
    }


async def run_chat(payload: ChatRequest, api_key: str | None = None) -> dict:
    conversation = rows("SELECT * FROM conversations WHERE id=?", (payload.conversation_id,))
    if not conversation:
        raise HTTPException(404, "对话不存在")
    convo = conversation[0]
    lock = _conversation_locks.setdefault(payload.conversation_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "该对话已有任务正在运行")

    task_id = payload.task_id or uuid.uuid4().hex
    task_started = time.monotonic()
    current_task = asyncio.current_task()
    if current_task is not None:
        _running_tasks[task_id] = current_task

    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, payload.conversation_id, "running", payload.content, now_iso(), now_iso()),
        )
        if not payload.approved_actions:
            db.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
                (payload.conversation_id, "user", payload.content, now_iso()),
            )
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), payload.conversation_id))

    model_calls = tool_call_count = files_modified = 0
    try:
        async with lock:
            await compact_conversation(payload.conversation_id, api_key)
            servers = rows("SELECT * FROM mcp_servers WHERE enabled=1 ORDER BY name")
            mcp_tools, mcp_routes = await discover_mcp_tools(servers, settings.allow_local_mcp)
            tools = [*BASE_TOOLS, *mcp_tools]
            skill_notes = skill_context(convo["workspace"], payload.content)
            system = (
                f"你是通用 Agent。工作区是 {convo['workspace']}。权限模式是 {convo['permission_mode']}。"
                "只能使用提供的工具操作工作区；先检查再修改，操作后验证。不能声称执行了未执行的操作。"
                + (f"\n\n{skill_notes}" if skill_notes else "")
            )
            model_messages = [{"role": "system", "content": system}, *model_history(payload.conversation_id)]
            pending: list[dict] = []
            signatures: Counter[str] = Counter()
            consecutive_failures = 0

            for _ in range(MAX_AGENT_ROUNDS):
                if time.monotonic() - task_started > TASK_TIMEOUT_SECONDS:
                    _task_update(task_id, "timed_out", termination_reason="任务超过 5 分钟", model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified)
                    return {"content": "任务因超过 5 分钟而终止。已完成的操作已保留，未完成步骤未继续执行。", "pending_actions": [], "task_id": task_id, "task_status": "timed_out"}

                message = await completion(model_messages, api_key, tools=tools)
                model_calls += 1
                message.pop("_metrics", None)
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    content = message.get("content") or ""
                    with connect() as db:
                        db.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)", (payload.conversation_id, "assistant", content, now_iso()))
                    _task_update(task_id, "completed", termination_reason="模型完成回答", model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified)
                    return {"content": content, "pending_actions": pending, "context": context_stats(payload.conversation_id), "task_id": task_id, "task_status": "completed"}

                model_messages.append(message)
                for call in tool_calls:
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
                    if signatures[signature] >= 3:
                        reason = f"检测到重复工具调用：{name}"
                        _task_update(task_id, "partially_completed", termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, last_error=reason)
                        return {"content": f"任务已停止：{reason}。已完成 {tool_call_count - 1} 次工具调用，文件修改 {files_modified} 次；其余步骤未继续。", "pending_actions": [], "task_id": task_id, "task_status": "partially_completed"}

                    if name in mcp_routes:
                        confirmed = approval_key(name, arguments) in payload.approved_actions
                        if requires_confirmation(convo["permission_mode"], "critical") and not confirmed:
                            result = {"success": False, "status": "confirmation_required", "approval_key": approval_key(name, arguments), "tool": name, "risk": "critical", "source": "mcp", "arguments": arguments, "impact": "外部 MCP 服务"}
                        else:
                            data = await invoke_mcp_route(mcp_routes[name], arguments, settings.allow_local_mcp)
                            result = {"success": True, "status": "ok", "data": data, "result": data}
                        risk, source = "critical", "mcp"
                    else:
                        key = approval_key(name, arguments)
                        confirmed = key in payload.approved_actions
                        result = await execute_command_async(convo["workspace"], convo["permission_mode"], arguments, confirmed) if name == "run_command" else execute_tool(convo["workspace"], convo["permission_mode"], name, arguments, confirmed)
                        risk, source = REGISTRY.get(name).risk if REGISTRY.get(name) else "critical", "builtin"

                    _record_run(payload.conversation_id, task_id, name, arguments, result, started, started_perf, risk, confirmed, source)
                    if result.get("success") and name in {"write_file", "copy_file", "move_file", "delete_file", "undo_file_change"}:
                        files_modified += 1
                    consecutive_failures = 0 if result.get("success") or result.get("status") == "confirmation_required" else consecutive_failures + 1
                    audit(payload.conversation_id, name, str(arguments.get("path") or arguments.get("source") or arguments.get("command") or ""), result.get("status", "ok"), arguments)
                    if result.get("status") == "confirmation_required":
                        pending.append(result)
                    model_messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})

                if pending:
                    _task_update(task_id, "waiting_confirmation", termination_reason="等待用户确认", model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified)
                    return {"content": "以下操作需要你的确认。", "pending_actions": pending, "context": context_stats(payload.conversation_id), "task_id": task_id, "task_status": "waiting_confirmation"}
                if consecutive_failures >= 3:
                    raise ValueError("工具连续失败 3 次")
            raise ValueError("Agent 工具循环超过 12 轮")
    except asyncio.CancelledError:
        _task_update(task_id, "cancelled", termination_reason="用户主动取消", model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified)
        audit(payload.conversation_id, "chat_cancel", "model", "cancelled", {"task_id": task_id})
        return _cancelled_result(task_id)
    except HTTPException:
        raise
    except Exception as exc:
        _task_update(task_id, "failed", termination_reason="执行异常", last_error=str(exc))
        audit(payload.conversation_id, "chat", "model", "error", {"error": str(exc)})
        raise HTTPException(502, str(exc)) from exc
    finally:
        _running_tasks.pop(task_id, None)
