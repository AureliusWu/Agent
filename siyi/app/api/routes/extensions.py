import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.database import audit, connect, now_iso, rows
from app.data_flow import record_data_flow
from app.extensions.runtime import install_extension, list_extensions, rollback_extension, set_extension_enabled
from app.tools.mcp import call_http_mcp, call_stdio_mcp_async, discover_mcp_tools
from app.security.network_security import NetworkPolicyError, validate_outbound_url
from app.permissions import authorize
from app.security.request_security import require_task_scope
from app.sandbox import safe_path, workspace_root
from app.schemas import EnabledUpdate, ExtensionInstallRequest, McpCall, McpServerCreate
from app.tools.skills import discover_skills, install_skill
from app.workspace.snapshots import SnapshotError, create_security_snapshot
from app.security.trust import redact_payload, secure_untrusted_payload

router = APIRouter(prefix="/api", tags=["extensions"])


def _mcp_public(item: dict) -> dict:
    result = dict(item)
    result.pop("command", None)
    result.pop("args", None)
    if result.get("url"):
        parsed = urlsplit(str(result["url"]))
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        query = urlencode([
            (key, "***REDACTED***" if any(marker in key.lower() for marker in ("token", "key", "secret", "password", "auth")) else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ])
        result["url"] = urlunsplit((parsed.scheme, host, parsed.path, query, parsed.fragment))
    try:
        result["tool_names"] = json.loads(result.get("tool_names") or "[]")
    except (TypeError, ValueError):
        result["tool_names"] = []
    result["enabled"] = bool(result.get("enabled"))
    return result


def _mcp_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    cleaned, _ = redact_payload({"error": message})
    return str(cleaned["error"])[:500]


async def _probe_mcp(item: dict) -> tuple[list[dict], str | None]:
    try:
        tools, _ = await discover_mcp_tools([item], settings.allow_local_mcp)
        if not tools:
            return [], "服务未返回可用工具"
        return tools, None
    except Exception as exc:
        return [], _mcp_error(exc)


def _store_mcp_health(server_id: int, tools: list[dict], error: str | None) -> None:
    names = [str((item.get("function") or {}).get("name") or "") for item in tools]
    names = [name for name in names if name]
    with connect() as db:
        db.execute(
            "UPDATE mcp_servers SET health_status=?, tool_count=?, tool_names=?, last_error=?, last_checked_at=? WHERE id=?",
            ("healthy" if tools and not error else "error", len(names), json.dumps(names, ensure_ascii=False), error, now_iso(), server_id),
        )


@router.get("/extensions/packages")
def extension_packages() -> list[dict]:
    return list_extensions()


@router.post("/extensions/packages")
def add_extension_package(payload: ExtensionInstallRequest) -> dict:
    try:
        result = install_extension(payload.workspace, payload.source_path, enable=payload.enable)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "extension_install", result["extension_id"], "ok", {"version": result["version"], "digest": result["digest"], "enabled": result["enabled"]})
    return result


@router.patch("/extensions/packages/{extension_id}/{version}/enabled")
def update_extension_package(extension_id: str, version: str, payload: EnabledUpdate) -> dict:
    try:
        result = set_extension_enabled(extension_id, version, payload.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "extension_enabled", extension_id, "ok", {"version": version, "enabled": payload.enabled})
    return result


@router.post("/extensions/packages/{extension_id}/rollback")
def rollback_extension_package(extension_id: str) -> dict:
    try:
        result = rollback_extension(extension_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "extension_rollback", extension_id, "ok", {"version": result["version"]})
    return result


@router.get("/skills")
def skills(workspace: str) -> list[dict]:
    return discover_skills(workspace)


@router.post("/skills")
def add_skill(workspace: str, name: str, content: str) -> dict:
    try:
        result = install_skill(workspace, name, content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "install_skill", result["path"], "ok")
    return result


@router.patch("/skills/enabled")
def update_skill(workspace: str, path: str, payload: EnabledUpdate) -> dict:
    root = workspace_root(workspace)
    if path.startswith("extension:"):
        if not any(item["path"] == path for item in discover_skills(workspace)):
            raise HTTPException(404, "扩展 Skill 不存在")
        key = path
    else:
        safe_path(root, path, must_exist=True)
        key = f"{root}|{path}"
    with connect() as db:
        db.execute("INSERT INTO skill_settings(path, enabled, updated_at) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at", (key, int(payload.enabled), now_iso()))
    return {"path": path, "enabled": payload.enabled}


@router.get("/mcp")
def mcp_servers() -> list[dict]:
    return [_mcp_public(item) for item in rows("SELECT * FROM mcp_servers ORDER BY name")]


@router.post("/mcp")
async def add_mcp(payload: McpServerCreate) -> dict:
    if payload.transport in {"http", "sse"} and not payload.url:
        raise HTTPException(400, "HTTP/SSE MCP 需要 URL")
    if payload.transport in {"http", "sse"}:
        try:
            await validate_outbound_url(payload.url or "", purpose="remote_mcp_registration", allow_private=settings.allow_local_mcp)
        except NetworkPolicyError as exc:
            raise HTTPException(400, str(exc)) from exc
    with connect() as db:
        cursor = db.execute("INSERT INTO mcp_servers(name, transport, url, command, args, enabled, created_at) VALUES(?,?,?,?,?,?,?)", (payload.name, payload.transport, payload.url, payload.command, json.dumps(payload.args, ensure_ascii=False), 0, now_iso()))
        server_id = int(cursor.lastrowid)
    item = rows("SELECT * FROM mcp_servers WHERE id=?", (server_id,))[0]
    tools, error = await _probe_mcp(item)
    _store_mcp_health(server_id, tools, error)
    if tools and not error:
        with connect() as db:
            db.execute("UPDATE mcp_servers SET enabled=1 WHERE id=?", (server_id,))
    return _mcp_public(rows("SELECT * FROM mcp_servers WHERE id=?", (server_id,))[0])


@router.patch("/mcp/{server_id}/enabled")
async def update_mcp(server_id: int, payload: EnabledUpdate) -> dict:
    server = rows("SELECT * FROM mcp_servers WHERE id=?", (server_id,))
    if not server:
        raise HTTPException(404, "MCP 服务不存在")
    if payload.enabled:
        tools, error = await _probe_mcp(server[0])
        _store_mcp_health(server_id, tools, error)
        if error or not tools:
            raise HTTPException(409, f"MCP 服务未通过工具发现：{error or '没有可用工具'}")
    with connect() as db:
        cursor = db.execute("UPDATE mcp_servers SET enabled=? WHERE id=?", (int(payload.enabled), server_id))
        if not cursor.rowcount:
            raise HTTPException(404, "MCP 服务不存在")
    return _mcp_public(rows("SELECT * FROM mcp_servers WHERE id=?", (server_id,))[0])


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
    tools, error = await _probe_mcp(server[0])
    _store_mcp_health(server_id, tools, error)
    return {
        "status": "ok" if tools and not error else "error",
        "tool_count": len(tools),
        "tools": [item["function"]["name"] for item in tools],
        "error": error,
    }


@router.get("/capabilities/runtime")
def runtime_capabilities(workspace: str = "") -> dict:
    workspace_selected = bool(workspace.strip())
    servers = [_mcp_public(item) for item in rows("SELECT * FROM mcp_servers ORDER BY name")]
    healthy = [item for item in servers if item["enabled"] and item.get("health_status") == "healthy" and item.get("tool_count", 0) > 0]
    return {
        "workspace_selected": workspace_selected,
        "capabilities": [
            {"id": "conversation", "label": "无工作区聊天", "status": "available", "reason": "身份、记忆与普通对话不依赖工作区"},
            {"id": "workspace_tools", "label": "工作区文件与命令", "status": "available" if workspace_selected else "unavailable", "reason": "已选择工作区" if workspace_selected else "需要用户主动选择工作区"},
            {"id": "remote_mcp", "label": "远程 MCP", "status": "available" if healthy else "unconfigured", "reason": f"{len(healthy)} 个服务已通过真实工具发现" if healthy else "没有已启用且通过工具发现的服务"},
        ],
        "mcp": {"configured": len(servers), "available": len(healthy), "servers": servers},
    }


@router.post("/mcp/call")
async def call_mcp(payload: McpCall) -> dict:
    server = rows("SELECT * FROM mcp_servers WHERE id=? AND enabled=1", (payload.server_id,))
    if not server:
        raise HTTPException(404, "MCP 服务不存在或未启用")
    conversation = rows("SELECT * FROM conversations WHERE id=?", (payload.conversation_id,))
    if not conversation:
        raise HTTPException(404, "对话不存在")
    require_task_scope(payload.conversation_id, payload.task_id)
    item = server[0]
    tool_name = f"mcp__{payload.server_id}__{payload.method}"
    decision = authorize(mode=conversation[0]["permission_mode"], risk="critical", tool=tool_name, arguments=payload.params, conversation_id=payload.conversation_id, task_id=payload.task_id, approval_tokens=payload.approval_tokens, approval_scope=payload.approval_scope, source="mcp", impact=item["name"], workspace=conversation[0]["workspace"])
    if not decision.allowed:
        return decision.confirmation or {"success": False, "status": "confirmation_required"}
    try:
        _, outbound_sensitive = redact_payload(payload.params)
        if outbound_sensitive.redactions:
            record_data_flow(source="api_client", sink=f"mcp:{item['name']}", classification="credential", fields=("params",), redactions=outbound_sensitive.redactions, allowed=False, reason="credential-bearing MCP arguments require a dedicated secret binding", conversation_id=payload.conversation_id, task_id=payload.task_id)
            raise HTTPException(409, "MCP 参数包含凭据，已阻止发送；请使用专用密钥绑定")
        record_data_flow(source="api_client", sink=f"mcp:{item['name']}", classification="internal", fields=("params",), allowed=True, reason="approved MCP call", conversation_id=payload.conversation_id, task_id=payload.task_id)
        snapshot = create_security_snapshot(conversation[0]["workspace"], reason=f"before_mcp:{tool_name}", conversation_id=payload.conversation_id, task_id=payload.task_id)
        result = await call_http_mcp(item["url"], payload.method, payload.params, allow_private=settings.allow_local_mcp) if item["transport"] in {"http", "sse"} else await call_stdio_mcp_async(item["command"], json.loads(item["args"] or "[]"), payload.method, payload.params)
        result, sensitive, findings = secure_untrusted_payload(result, f"mcp:{item['name']}:{payload.method}")
        record_data_flow(
            source=f"mcp:{item['name']}",
            sink="api_client",
            classification=sensitive.classification,
            fields=("mcp_result",),
            redactions=sensitive.redactions,
            allowed=True,
            reason=f"untrusted MCP result; injection findings: {','.join(findings)}" if findings else "untrusted MCP result",
            conversation_id=payload.conversation_id,
            task_id=payload.task_id,
        )
        audit(payload.conversation_id, "mcp_call", item["name"], "ok", {"method": payload.method})
        if findings:
            audit(payload.conversation_id, "prompt_injection_detected", item["name"], "blocked_as_instruction", {"findings": findings, "source": "mcp"})
        result["security_snapshot_id"] = snapshot["id"]
        return result
    except HTTPException:
        raise
    except SnapshotError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        audit(payload.conversation_id, "mcp_call", item["name"], "error", {"error": str(exc)})
        raise HTTPException(502, str(exc)) from exc
