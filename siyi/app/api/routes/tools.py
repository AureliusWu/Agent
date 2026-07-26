from __future__ import annotations

import subprocess

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.database import audit
from app.memory.service import MEMORY_TOOLS, execute_memory_tool
from app.permissions import authorize
from app.security.request_security import require_conversation_scope, require_task_scope
from app.sandbox import SandboxError, execute_tool, write_uploaded_file
from app.schemas import ToolRequest
from app.tools.registry import REGISTRY

router = APIRouter(prefix="/api", tags=["tools"])


@router.post("/tools/execute")
def run_tool(payload: ToolRequest) -> dict:
    try:
        scope = require_conversation_scope(
            payload.conversation_id,
            workspace=payload.workspace,
            permission_mode=payload.permission_mode,
        )
        require_task_scope(scope.conversation_id, payload.task_id)
        if payload.tool in MEMORY_TOOLS:
            spec = REGISTRY[payload.tool]
            decision = authorize(mode=scope.permission_mode, risk=spec.risk, tool=payload.tool, arguments=payload.arguments, conversation_id=scope.conversation_id, task_id=payload.task_id, approval_tokens=payload.approval_tokens, approval_scope=payload.approval_scope, impact="当前工作区长期记忆", workspace=scope.workspace)
            result = execute_memory_tool(scope.workspace, payload.tool, payload.arguments, payload.task_id) if decision.allowed else decision.confirmation
        else:
            result = execute_tool(scope.workspace, scope.permission_mode, payload.tool, payload.arguments, payload.approval_tokens, approval_scope=payload.approval_scope, conversation_id=scope.conversation_id, task_id=payload.task_id)
        audit(scope.conversation_id, payload.tool, str(payload.arguments.get("path") or payload.arguments.get("source") or ""), result["status"], payload.arguments)
        return result
    except (SandboxError, OSError, KeyError, subprocess.SubprocessError) as exc:
        audit(payload.conversation_id, payload.tool, "", "error", {"error": str(exc)})
        raise HTTPException(400, str(exc)) from exc


@router.post("/files/upload")
async def upload_file(workspace: str, path: str, conversation_id: int, permission_mode: str = "ask", task_id: str | None = None, approval_token: str | None = None, approval_scope: str = "once", file: UploadFile = File(...)) -> dict:
    scope = require_conversation_scope(conversation_id, workspace=workspace, permission_mode=permission_mode)
    require_task_scope(scope.conversation_id, task_id)
    content = await file.read()
    if len(content) > 20_000_000:
        raise HTTPException(413, "文件超过 20 MB")
    result = write_uploaded_file(scope.workspace, scope.permission_mode, path, content, [approval_token] if approval_token else [], approval_scope=approval_scope, conversation_id=scope.conversation_id, task_id=task_id)
    audit(scope.conversation_id, "upload_file", path, result["status"], {"bytes": len(content)})
    return result
