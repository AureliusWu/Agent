import json

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..database import audit, connect, now_iso, rows
from ..mcp import call_http_mcp, call_stdio_mcp_async, discover_mcp_tools
from ..sandbox import safe_path, workspace_root
from ..schemas import EnabledUpdate, McpCall, McpServerCreate
from ..skills import discover_skills, install_skill

router = APIRouter(prefix="/api", tags=["extensions"])


@router.get("/skills")
def skills(workspace: str) -> list[dict]:
    return discover_skills(workspace)


@router.post("/skills")
def add_skill(workspace: str, name: str, content: str) -> dict:
    result = install_skill(workspace, name, content)
    audit(None, "install_skill", result["path"], "ok")
    return result


@router.patch("/skills/enabled")
def update_skill(workspace: str, path: str, payload: EnabledUpdate) -> dict:
    root = workspace_root(workspace)
    safe_path(root, path, must_exist=True)
    key = f"{root}|{path}"
    with connect() as db:
        db.execute("INSERT INTO skill_settings(path, enabled, updated_at) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at", (key, int(payload.enabled), now_iso()))
    return {"path": path, "enabled": payload.enabled}


@router.get("/mcp")
def mcp_servers() -> list[dict]:
    return rows("SELECT * FROM mcp_servers ORDER BY name")


@router.post("/mcp")
def add_mcp(payload: McpServerCreate) -> dict:
    if payload.transport in {"http", "sse"} and not payload.url:
        raise HTTPException(400, "HTTP/SSE MCP 需要 URL")
    with connect() as db:
        cursor = db.execute("INSERT INTO mcp_servers(name, transport, url, command, args, created_at) VALUES(?,?,?,?,?,?)", (payload.name, payload.transport, payload.url, payload.command, json.dumps(payload.args, ensure_ascii=False), now_iso()))
    return {"id": cursor.lastrowid, **payload.model_dump()}


@router.patch("/mcp/{server_id}/enabled")
def update_mcp(server_id: int, payload: EnabledUpdate) -> dict:
    with connect() as db:
        cursor = db.execute("UPDATE mcp_servers SET enabled=? WHERE id=?", (int(payload.enabled), server_id))
        if not cursor.rowcount:
            raise HTTPException(404, "MCP 服务不存在")
    return {"id": server_id, "enabled": payload.enabled}


@router.delete("/mcp/{server_id}")
def delete_mcp(server_id: int) -> dict:
    with connect() as db:
        cursor = db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
        if not cursor.rowcount:
            raise HTTPException(404, "MCP 服务不存在")
    return {"id": server_id, "deleted": True}


@router.post("/mcp/{server_id}/test")
async def test_mcp(server_id: int) -> dict:
    server = rows("SELECT * FROM mcp_servers WHERE id=?", (server_id,))
    if not server:
        raise HTTPException(404, "MCP 服务不存在")
    tools, _ = await discover_mcp_tools(server, settings.allow_local_mcp)
    return {"status": "ok" if tools else "error", "tool_count": len(tools), "tools": [item["function"]["name"] for item in tools]}


@router.post("/mcp/call")
async def call_mcp(payload: McpCall) -> dict:
    server = rows("SELECT * FROM mcp_servers WHERE id=? AND enabled=1", (payload.server_id,))
    if not server:
        raise HTTPException(404, "MCP 服务不存在或未启用")
    item = server[0]
    try:
        result = await call_http_mcp(item["url"], payload.method, payload.params) if item["transport"] in {"http", "sse"} else await call_stdio_mcp_async(item["command"], json.loads(item["args"] or "[]"), payload.method, payload.params)
        audit(None, "mcp_call", item["name"], "ok", {"method": payload.method})
        return result
    except Exception as exc:
        audit(None, "mcp_call", item["name"], "error", {"error": str(exc)})
        raise HTTPException(502, str(exc)) from exc
