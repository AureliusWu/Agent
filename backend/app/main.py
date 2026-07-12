from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .context import compact_conversation, context_stats, model_history
from .database import audit, connect, init_db, now_iso, rows
from .mcp import call_http_mcp, call_stdio_mcp, discover_mcp_tools, invoke_mcp_route
from .provider import BASE_TOOLS, completion, provider_health
from .sandbox import SandboxError, approval_key, execute_tool, safe_path, workspace_root
from .schemas import ChatRequest, CompactRequest, ConversationCreate, McpCall, McpServerCreate, PermissionUpdate, ToolRequest
from .skills import discover_skills, install_skill, skill_context


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Agent API", version="0.2.0", lifespan=lifespan)
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


@app.patch("/api/conversations/{conversation_id}/permission")
def update_permission(conversation_id: int, payload: PermissionUpdate) -> dict:
    with connect() as db:
        cursor = db.execute("UPDATE conversations SET permission_mode=?, updated_at=? WHERE id=?", (payload.permission_mode, now_iso(), conversation_id))
        if not cursor.rowcount:
            raise HTTPException(404, "对话不存在")
    audit(conversation_id, "permission_mode", payload.permission_mode, "ok")
    return {"id": conversation_id, "permission_mode": payload.permission_mode}


def _record_run(conversation_id: int, tool: str, arguments: dict, result: dict, started: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO tool_runs(conversation_id, tool, status, input, output, started_at, finished_at) VALUES(?,?,?,?,?,?,?)", (conversation_id, tool, result.get("status", "ok"), json.dumps(arguments, ensure_ascii=False), json.dumps(result, ensure_ascii=False)[:40_000], started, now_iso()))


@app.post("/api/chat")
async def chat(payload: ChatRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    conversation = rows("SELECT * FROM conversations WHERE id=?", (payload.conversation_id,))
    if not conversation:
        raise HTTPException(404, "对话不存在")
    convo = conversation[0]
    # Approval retries should continue the existing turn instead of duplicating the user message.
    if not payload.approved_actions:
        with connect() as db:
            db.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)", (payload.conversation_id, "user", payload.content, now_iso()))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), payload.conversation_id))
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
        for _ in range(12):
            message = await completion(model_messages, x_model_api_key, tools=tools)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = message.get("content") or ""
                with connect() as db:
                    db.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)", (payload.conversation_id, "assistant", content, now_iso()))
                return {"content": content, "pending_actions": pending, "context": context_stats(payload.conversation_id)}
            model_messages.append(message)
            for call in tool_calls:
                function = call["function"]
                arguments = json.loads(function.get("arguments") or "{}")
                name, started = function["name"], now_iso()
                if name in mcp_routes:
                    if convo["permission_mode"] == "readonly":
                        result = {"status": "denied", "error": "只读模式禁止调用外部 MCP"}
                    elif convo["permission_mode"] == "confirm" and approval_key(name, arguments) not in payload.approved_actions:
                        result = {"status": "confirmation_required", "approval_key": approval_key(name, arguments), "tool": name, "arguments": arguments}
                    else:
                        result = await invoke_mcp_route(mcp_routes[name], arguments, settings.allow_local_mcp)
                        result = {"status": "ok", "result": result}
                else:
                    key = approval_key(name, arguments)
                    result = execute_tool(convo["workspace"], convo["permission_mode"], name, arguments, key in payload.approved_actions)
                _record_run(payload.conversation_id, name, arguments, result, started)
                audit(payload.conversation_id, name, str(arguments.get("path") or arguments.get("source") or arguments.get("command") or ""), result.get("status", "ok"), arguments)
                if result.get("status") == "confirmation_required":
                    pending.append(result)
                model_messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            if pending:
                return {"content": "以下操作需要你的确认。", "pending_actions": pending, "context": context_stats(payload.conversation_id)}
        raise ValueError("Agent 工具循环超过 12 轮")
    except Exception as exc:
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
async def upload_file(workspace: str, path: str, permission_mode: str = "confirm", approved: bool = False, file: UploadFile = File(...)) -> dict:
    if permission_mode == "readonly":
        raise HTTPException(403, "只读模式禁止上传")
    if permission_mode == "confirm" and not approved:
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


@app.get("/api/mcp")
def mcp_servers() -> list[dict]: return rows("SELECT * FROM mcp_servers ORDER BY name")


@app.post("/api/mcp")
def add_mcp(payload: McpServerCreate) -> dict:
    if payload.transport in {"http", "sse"} and not payload.url:
        raise HTTPException(400, "HTTP/SSE MCP 需要 URL")
    with connect() as db:
        cursor = db.execute("INSERT INTO mcp_servers(name, transport, url, command, args, created_at) VALUES(?,?,?,?,?,?)", (payload.name, payload.transport, payload.url, payload.command, json.dumps(payload.args, ensure_ascii=False), now_iso()))
    return {"id": cursor.lastrowid, **payload.model_dump()}


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
