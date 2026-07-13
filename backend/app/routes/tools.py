from __future__ import annotations

import subprocess

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..database import audit
from ..sandbox import SandboxError, execute_tool, safe_path, workspace_root
from ..schemas import ToolRequest

router = APIRouter(prefix="/api", tags=["tools"])


@router.post("/tools/execute")
def run_tool(payload: ToolRequest) -> dict:
    try:
        result = execute_tool(payload.workspace, payload.permission_mode, payload.tool, payload.arguments, payload.approved)
        audit(payload.conversation_id, payload.tool, str(payload.arguments.get("path") or payload.arguments.get("source") or ""), result["status"], payload.arguments)
        return result
    except (SandboxError, OSError, KeyError, subprocess.SubprocessError) as exc:
        audit(payload.conversation_id, payload.tool, "", "error", {"error": str(exc)})
        raise HTTPException(400, str(exc)) from exc


@router.post("/files/upload")
async def upload_file(workspace: str, path: str, permission_mode: str = "ask", approved: bool = False, file: UploadFile = File(...)) -> dict:
    if permission_mode == "ask" and not approved:
        return {"status": "confirmation_required", "approval_key": f"upload_file:{path}", "filename": file.filename}
    root = workspace_root(workspace)
    target = safe_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    if len(content) > 20_000_000:
        raise HTTPException(413, "文件超过 20 MB")
    target.write_bytes(content)
    audit(None, "upload_file", str(target.relative_to(root)), "ok", {"bytes": len(content)})
    return {"status": "ok", "path": str(target.relative_to(root)), "bytes": len(content)}
