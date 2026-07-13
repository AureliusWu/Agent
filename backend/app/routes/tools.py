from __future__ import annotations

import subprocess

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..database import audit
from ..memory import MEMORY_TOOLS, execute_memory_tool
from ..permissions import authorize
from ..sandbox import SandboxError, execute_tool, write_uploaded_file
from ..schemas import ToolRequest
from ..tool_registry import REGISTRY

router = APIRouter(prefix="/api", tags=["tools"])


@router.post("/tools/execute")
def run_tool(payload: ToolRequest) -> dict:
    try:
        if payload.tool in MEMORY_TOOLS:
            spec = REGISTRY[payload.tool]
            decision = authorize(mode=payload.permission_mode, risk=spec.risk, tool=payload.tool, arguments=payload.arguments, conversation_id=payload.conversation_id, task_id=payload.task_id, approval_tokens=payload.approval_tokens, approval_scope=payload.approval_scope, impact="当前工作区长期记忆")
            result = execute_memory_tool(payload.workspace, payload.tool, payload.arguments, payload.task_id) if decision.allowed else decision.confirmation
        else:
            result = execute_tool(payload.workspace, payload.permission_mode, payload.tool, payload.arguments, payload.approval_tokens, approval_scope=payload.approval_scope, conversation_id=payload.conversation_id, task_id=payload.task_id)
        audit(payload.conversation_id, payload.tool, str(payload.arguments.get("path") or payload.arguments.get("source") or ""), result["status"], payload.arguments)
        return result
    except (SandboxError, OSError, KeyError, subprocess.SubprocessError) as exc:
        audit(payload.conversation_id, payload.tool, "", "error", {"error": str(exc)})
        raise HTTPException(400, str(exc)) from exc


@router.post("/files/upload")
async def upload_file(workspace: str, path: str, permission_mode: str = "ask", conversation_id: int | None = None, task_id: str | None = None, approval_token: str | None = None, approval_scope: str = "once", file: UploadFile = File(...)) -> dict:
    content = await file.read()
    if len(content) > 20_000_000:
        raise HTTPException(413, "文件超过 20 MB")
    result = write_uploaded_file(workspace, permission_mode, path, content, [approval_token] if approval_token else [], approval_scope=approval_scope, conversation_id=conversation_id, task_id=task_id)
    audit(conversation_id, "upload_file", path, result["status"], {"bytes": len(content)})
    return result
