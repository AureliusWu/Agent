from __future__ import annotations

import json
import subprocess
import asyncio
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .context import compact_conversation, context_stats, model_history
from .database import audit, backup_database, connect, database_backups, init_db, now_iso, restore_database, rows, sanitize_details
from .mcp import call_http_mcp, call_stdio_mcp, discover_mcp_tools, invoke_mcp_route
from .provider import completion, provider_health
from .sandbox import SandboxError, approval_key, execute_tool, safe_path, workspace_root
from .schemas import ChatRequest, CompactRequest, ConversationCreate, ConversationRename, EnabledUpdate, McpCall, McpServerCreate, PermissionUpdate, ToolRequest
from .skills import discover_skills, install_skill, skill_context
from .tool_registry import BASE_TOOLS, REGISTRY, requires_confirmation


TASK_TIMEOUT_SECONDS = 300
_conversation_locks: dict[int, asyncio.Lock] = {}
_cancelled_tasks: set[str] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Agent API", version="0.3.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": app.version, "database": str(settings.database_path), "model": settings.model_name}


@app.get("/api/provider/health")
async def model_health(x_model_api_key: str | None = Header(default=None)) -> dict:
    return await provider_health(x_model_api_key)


@app.get("/api/conversations")
def conversations() -> list[dict]:
    return rows("SELECT * FROM conversations ORDER BY updated_at DESC")


@app.patch("/api/conversations/{conversation_id}")
def rename_conversation(conversation_id: int, payload: ConversationRename) -> dict:
    with connect() as db:
        cursor = db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?", (payload.title.strip(), now_iso(), conversation_id))
        if not cursor.rowcount: raise HTTPException(404, "对话不存在")
    return {"id": conversation_id, "title": payload.title.strip()}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: int) -> dict:
    with connect() as db:
        cursor = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        if not cursor.rowcount: raise HTTPException(404, "对话不存在")
    return {"deleted": True, "id": conversation_id}


@app.post("/api/conversations")
def create_conversation(payload: ConversationCreate) -> dict:
    root, now = workspace_root(payload.workspace), now_iso()
    with connect() as db:
        cursor = db.execute("INSERT INTO conversations(title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?)", (payload.title, str(root), payload.permission_mode, now, now))
    return {"id": cursor.lastrowid, "title": payload.title, "workspace": str(root), "permission_mode": payload.permission_mode, "created_at": now, "updated_at": now}


@app.get("/api/conversations/{conversation_id}/messages")
def messages(conversation_id: int) -> list[dict]:
    return rows("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,))


@app.get("/api/conversations/{conversation_id}/context")
def conversation_context(conversation_id: int) -> dict:
    return context_stats(conversation_id)


@app.post("/api/conversations/{conversation_id}/compact")
async def compact(conversation_id: int, payload: CompactRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    try:
        result = await compact_conversation(conversation_id, x_model_api_key, force=payload.force)
        audit(conversation_id, "compact_context", "conversation", "ok", result)
        return result
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/conversations/{conversation_id}/runs")
def tool_runs(conversation_id: int) -> list[dict]:
    return rows("SELECT * FROM tool_runs WHERE conversation_id=? ORDER BY id DESC LIMIT 100", (conversation_id,))


@app.get("/api/conversations/{conversation_id}/tasks")
def tasks(conversation_id: int) -> list[dict]:
    return rows("SELECT * FROM agent_tasks WHERE conversation_id=? ORDER BY created_at DESC LIMIT 100", (conversation_id,))


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict:
    task = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not task: raise HTTPException(404, "任务不存在")
    _cancelled_tasks.add(task_id)
    with connect() as db:
        db.execute("UPDATE agent_tasks SET status='cancelled', termination_reason='用户主动取消', updated_at=? WHERE id=?", (now_iso(), task_id))
    return {"id": task_id, "status": "cancelled"}


@app.patch("/api/conversations/{conversation_id}/permission")
def update_permission(conversation_id: int, payload: PermissionUpdate) -> dict:
    with connect() as db:
        cursor = db.execute("UPDATE conversations SET permission_mode=?, updated_at=? WHERE id=?", (payload.permission_mode, now_iso(), conversation_id))
        if not cursor.rowcount:
            raise HTTPException(404, "对话不存在")
    audit(conversation_id, "permission_mode", payload.permission_mode, "ok")
    return {"id": conversation_id, "permission_mode": payload.permission_mode}


def _record_run(conversation_id: int, task_id: str, tool: str, arguments: dict, result: dict, started: str, started_perf: float, risk: str, confirmed: bool, source: str = "builtin") -> None:
    with connect() as db:
        db.execute("INSERT INTO tool_runs(conversation_id, task_id, source, risk, confirmed, tool, status, input, output, started_at, finished_at, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (conversation_id, task_id, source, risk, int(confirmed), tool, result.get("status", "ok"), json.dumps(sanitize_details(arguments), ensure_ascii=False), json.dumps(sanitize_details(result), ensure_ascii=False)[:40_000], started, now_iso(), round((time.perf_counter() - started_perf) * 1000)))


def _task_update(task_id: str, status: str, **fields: object) -> None:
    allowed = {"termination_reason", "model_calls", "tool_calls", "files_modified", "last_error"}
    values = {key: value for key, value in fields.items() if key in allowed}
    assignments = ["status=?", "updated_at=?", *[f"{key}=?" for key in values]]
    with connect() as db:
        db.execute(f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE id=?", (status, now_iso(), *values.values(), task_id))


@app.post("/api/chat")
async def chat(payload: ChatRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    conversation = rows("SELECT * FROM conversations WHERE id=?", (payload.conversation_id,))
    if not conversation:
        raise HTTPException(404, "对话不存在")
    convo = conversation[0]
    lock = _conversation_locks.setdefault(payload.conversation_id, asyncio.Lock())
    if lock.locked(): raise HTTPException(409, "该对话已有任务正在运行")
    task_id, task_started = payload.task_id or uuid.uuid4().hex, time.monotonic()
    with connect() as db:
        db.execute("INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)", (task_id, payload.conversation_id, "running", payload.content, now_iso(), now_iso()))
    # Approval retries should continue the existing turn instead of duplicating the user message.
    if not payload.approved_actions:
        with connect() as db:
            db.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)", (payload.conversation_id, "user", payload.content, now_iso()))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), payload.conversation_id))
    async with lock:
      try:
        await compact_conversation(payload.conversation_id, x_model_api_key)
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
        signatures: Counter[str] = Counter(); model_calls = tool_call_count = files_modified = consecutive_failures = 0
        for _ in range(12):
            if task_id in _cancelled_tasks:
                _task_update(task_id, "cancelled", termination_reason="用户主动取消", model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified)
                return {"content": "任务已取消。已完成的文件操作保留，可在审计中查看并使用撤销工具恢复。", "pending_actions": [], "task_id": task_id, "task_status": "cancelled"}
            if time.monotonic() - task_started > TASK_TIMEOUT_SECONDS:
                _task_update(task_id, "timed_out", termination_reason="任务超过 5 分钟", model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified)
                return {"content": "任务因超过 5 分钟而终止。已完成的操作已保留，未完成步骤未继续执行。", "pending_actions": [], "task_id": task_id, "task_status": "timed_out"}
            message = await completion(model_messages, x_model_api_key, tools=tools)
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
                try: arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError: arguments = {"_invalid_json": function.get("arguments")}
                name, started, started_perf = function["name"], now_iso(), time.perf_counter(); tool_call_count += 1
                signature = f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"; signatures[signature] += 1
                if signatures[signature] >= 3:
                    reason = f"检测到重复工具调用：{name}"
                    _task_update(task_id, "partially_completed", termination_reason=reason, model_calls=model_calls, tool_calls=tool_call_count, files_modified=files_modified, last_error=reason)
                    return {"content": f"任务已停止：{reason}。已完成 {tool_call_count - 1} 次工具调用，文件修改 {files_modified} 次；其余步骤未继续。", "pending_actions": [], "task_id": task_id, "task_status": "partially_completed"}
                if name in mcp_routes:
                    confirmed = approval_key(name, arguments) in payload.approved_actions
                    if requires_confirmation(convo["permission_mode"], "critical") and not confirmed:
                        result = {"success": False, "status": "confirmation_required", "approval_key": approval_key(name, arguments), "tool": name, "risk": "critical", "source": "mcp", "arguments": arguments, "impact": "外部 MCP 服务"}
                    else:
                        result = await invoke_mcp_route(mcp_routes[name], arguments, settings.allow_local_mcp)
                        result = {"success": True, "status": "ok", "data": result, "result": result}
                    risk, source = "critical", "mcp"
                else:
                    key = approval_key(name, arguments)
                    result = execute_tool(convo["workspace"], convo["permission_mode"], name, arguments, key in payload.approved_actions)
                    risk, source, confirmed = REGISTRY.get(name).risk if REGISTRY.get(name) else "critical", "builtin", key in payload.approved_actions
                _record_run(payload.conversation_id, task_id, name, arguments, result, started, started_perf, risk, confirmed, source)
                if result.get("success") and name in {"write_file", "copy_file", "move_file", "delete_file", "undo_file_change"}: files_modified += 1
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
      except Exception as exc:
        _task_update(task_id, "failed", termination_reason="执行异常", last_error=str(exc))
        audit(payload.conversation_id, "chat", "model", "error", {"error": str(exc)})
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/tools/execute")
def run_tool(payload: ToolRequest) -> dict:
    try:
        result = execute_tool(payload.workspace, payload.permission_mode, payload.tool, payload.arguments, payload.approved)
        audit(payload.conversation_id, payload.tool, str(payload.arguments.get("path") or payload.arguments.get("source") or ""), result["status"], payload.arguments)
        return result
    except (SandboxError, OSError, KeyError, subprocess.SubprocessError) as exc:
        audit(payload.conversation_id, payload.tool, "", "error", {"error": str(exc)})
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/files/upload")
async def upload_file(workspace: str, path: str, permission_mode: str = "ask", approved: bool = False, file: UploadFile = File(...)) -> dict:
    if permission_mode == "ask" and not approved:
        return {"status": "confirmation_required", "approval_key": f"upload_file:{path}", "filename": file.filename}
    root, target = workspace_root(workspace), safe_path(workspace_root(workspace), path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    if len(content) > 20_000_000:
        raise HTTPException(413, "文件超过 20 MB")
    target.write_bytes(content); audit(None, "upload_file", str(target.relative_to(root)), "ok", {"bytes": len(content)})
    return {"status": "ok", "path": str(target.relative_to(root)), "bytes": len(content)}


@app.get("/api/skills")
def skills(workspace: str) -> list[dict]: return discover_skills(workspace)


@app.post("/api/skills")
def add_skill(workspace: str, name: str, content: str) -> dict:
    result = install_skill(workspace, name, content); audit(None, "install_skill", result["path"], "ok"); return result


@app.patch("/api/skills/enabled")
def update_skill(workspace: str, path: str, payload: EnabledUpdate) -> dict:
    root = workspace_root(workspace); safe_path(root, path, must_exist=True); key = f"{root}|{path}"
    with connect() as db:
        db.execute("INSERT INTO skill_settings(path, enabled, updated_at) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at", (key, int(payload.enabled), now_iso()))
    return {"path": path, "enabled": payload.enabled}


@app.get("/api/mcp")
def mcp_servers() -> list[dict]: return rows("SELECT * FROM mcp_servers ORDER BY name")


@app.post("/api/mcp")
def add_mcp(payload: McpServerCreate) -> dict:
    if payload.transport in {"http", "sse"} and not payload.url:
        raise HTTPException(400, "HTTP/SSE MCP 需要 URL")
    with connect() as db:
        cursor = db.execute("INSERT INTO mcp_servers(name, transport, url, command, args, created_at) VALUES(?,?,?,?,?,?)", (payload.name, payload.transport, payload.url, payload.command, json.dumps(payload.args, ensure_ascii=False), now_iso()))
    return {"id": cursor.lastrowid, **payload.model_dump()}


@app.patch("/api/mcp/{server_id}/enabled")
def update_mcp(server_id: int, payload: EnabledUpdate) -> dict:
    with connect() as db:
        cursor = db.execute("UPDATE mcp_servers SET enabled=? WHERE id=?", (int(payload.enabled), server_id))
        if not cursor.rowcount: raise HTTPException(404, "MCP 服务不存在")
    return {"id": server_id, "enabled": payload.enabled}


@app.delete("/api/mcp/{server_id}")
def delete_mcp(server_id: int) -> dict:
    with connect() as db:
        cursor = db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
        if not cursor.rowcount: raise HTTPException(404, "MCP 服务不存在")
    return {"id": server_id, "deleted": True}


@app.post("/api/mcp/{server_id}/test")
async def test_mcp(server_id: int) -> dict:
    server = rows("SELECT * FROM mcp_servers WHERE id=?", (server_id,))
    if not server: raise HTTPException(404, "MCP 服务不存在")
    tools, _ = await discover_mcp_tools(server, settings.allow_local_mcp)
    return {"status": "ok" if tools else "error", "tool_count": len(tools), "tools": [item["function"]["name"] for item in tools]}


@app.post("/api/mcp/call")
async def call_mcp(payload: McpCall) -> dict:
    server = rows("SELECT * FROM mcp_servers WHERE id=? AND enabled=1", (payload.server_id,))
    if not server:
        raise HTTPException(404, "MCP 服务不存在或未启用")
    item = server[0]
    try:
        result = await call_http_mcp(item["url"], payload.method, payload.params) if item["transport"] in {"http", "sse"} else call_stdio_mcp(item["command"], json.loads(item["args"] or "[]"), payload.method, payload.params)
        audit(None, "mcp_call", item["name"], "ok", {"method": payload.method}); return result
    except Exception as exc:
        audit(None, "mcp_call", item["name"], "error", {"error": str(exc)}); raise HTTPException(502, str(exc)) from exc


@app.get("/api/audit")
def audit_logs(limit: int = 100) -> list[dict]: return rows("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),))


@app.get("/api/database/backups")
def list_database_backups() -> list[dict]: return database_backups()


@app.post("/api/database/backups")
def create_database_backup() -> dict:
    result = backup_database(); audit(None, "database_backup", result["name"], "ok"); return result


@app.post("/api/database/restore/{name}")
def restore_database_backup(name: str) -> dict:
    try:
        result = restore_database(name); audit(None, "database_restore", name, "ok"); return result
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
