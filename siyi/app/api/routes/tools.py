from __future__ import annotations

import subprocess
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile, Query
from pydantic import BaseModel
from anyio import from_thread

from app.database import audit
from app.memory.service import MEMORY_TOOLS, execute_memory_tool
from app.permissions import authorize
from app.kernel.services import build_kernel_services
from app.runtime.executor import ExecutorToolCall
from app.security.request_security import require_conversation_scope, require_task_scope
from app.sandbox import SandboxError, execute_tool, write_uploaded_file
from app.schemas import ToolRequest
from app.tools.registry import REGISTRY, ToolValidationError, validate_arguments
from app.tools.file_operations import CORE_FILE_OPERATIONS, FileOperationRequest, execute_file_operation

router = APIRouter(prefix="/api", tags=["tools"])


class FileReconcileRequest(BaseModel):
    conversation_id: int


@router.get("/file-recovery")
def file_recovery_inventory(conversation_id: int, limit: int = Query(25, ge=1, le=50),
                            offset: int = Query(0, ge=0, le=100_000)) -> dict:
    from app.workspace.recovery_inventory import list_recovery

    scope = require_conversation_scope(conversation_id)
    return list_recovery(scope.workspace, limit=limit, offset=offset)


@router.get("/file-transactions")
def file_transactions(conversation_id: int, limit: int = Query(25, ge=1, le=50),
                      offset: int = Query(0, ge=0, le=100_000)) -> dict:
    from app.workspace.file_journal import list_transactions

    scope = require_conversation_scope(conversation_id)
    return list_transactions(scope.workspace, scope.conversation_id, limit=limit, offset=offset)


@router.get("/file-transactions/{operation_id}")
def file_transaction_detail(operation_id: str, conversation_id: int) -> dict:
    from app.workspace.file_journal import detail, JournalError

    scope = require_conversation_scope(conversation_id)
    try:
        return detail(operation_id, workspace=scope.workspace, conversation_id=scope.conversation_id)
    except JournalError as exc:
        raise HTTPException(404, {"code": exc.code, "message": str(exc)}) from exc


@router.post("/file-transactions/{operation_id}/reconcile")
def file_transaction_reconcile(operation_id: str, payload: FileReconcileRequest) -> dict:
    from app.workspace.file_journal import reconcile, JournalError

    scope = require_conversation_scope(payload.conversation_id)
    try:
        result = reconcile(operation_id, workspace=scope.workspace, conversation_id=scope.conversation_id)
        audit(scope.conversation_id, "file_transaction_reconcile", "", result["status"], {"operation_id": operation_id})
        return result
    except JournalError as exc:
        raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc


@router.post("/tools/execute")
def run_tool(payload: ToolRequest) -> dict:
    try:
        scope = require_conversation_scope(
            payload.conversation_id,
            workspace=payload.workspace,
            permission_mode=payload.permission_mode,
        )
        require_task_scope(scope.conversation_id, payload.task_id)
        if payload.tool in {"file_batch", "undo_file_batch"} or payload.tool in CORE_FILE_OPERATIONS:
            services = build_kernel_services()
            outcome = from_thread.run(services.executor.execute_tool, ExecutorToolCall(
                workspace=scope.workspace, mode=scope.permission_mode, name=payload.tool,
                arguments=payload.arguments, tool_call_id=f"api:{uuid.uuid4().hex}",
                approved_actions=payload.approval_tokens, approval_scope=payload.approval_scope,
                conversation_id=scope.conversation_id, task_id=payload.task_id,
                mcp_routes={}, permission_fn=services.permissions.authorize,
            ))
            result = outcome.result
        elif payload.tool in MEMORY_TOOLS:
            spec = REGISTRY[payload.tool]
            decision = authorize(mode=scope.permission_mode, risk=spec.risk, tool=payload.tool, arguments=payload.arguments, conversation_id=scope.conversation_id, task_id=payload.task_id, approval_tokens=payload.approval_tokens, approval_scope=payload.approval_scope, impact="当前工作区长期记忆", workspace=scope.workspace)
            result = execute_memory_tool(scope.workspace, payload.tool, payload.arguments, payload.task_id) if decision.allowed else decision.confirmation
        elif payload.tool.startswith("artifact."):
            from app.artifacts.service import ARTIFACT_TOOLS, execute_artifact_tool

            if payload.tool not in ARTIFACT_TOOLS:
                raise KeyError(payload.tool)
            spec = REGISTRY[payload.tool]
            try:
                validate_arguments(payload.tool, payload.arguments)
            except ToolValidationError as exc:
                result = {
                    "success": False,
                    "status": "error",
                    "error_code": "invalid_arguments",
                    "error_message": str(exc),
                }
            else:
                decision = authorize(
                    mode=scope.permission_mode,
                    risk=spec.risk,
                    tool=payload.tool,
                    arguments=payload.arguments,
                    conversation_id=scope.conversation_id,
                    task_id=payload.task_id,
                    approval_tokens=payload.approval_tokens,
                    approval_scope=payload.approval_scope,
                    impact=str(
                        payload.arguments.get("path")
                        or payload.arguments.get("output_directory")
                        or "current workspace artifact"
                    ),
                    workspace=scope.workspace,
                )
                result = (
                    execute_artifact_tool(
                        scope.workspace,
                        payload.tool,
                        payload.arguments,
                        task_id=payload.task_id,
                        tool_call_id=(
                            f"api:{payload.task_id}:{payload.tool}"
                            if payload.task_id
                            else None
                        ),
                    )
                    if decision.allowed
                    else decision.confirmation
                )
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
