from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from app.database import audit
from app.permissions import authorize
from app.schemas import VisionRequest
from app.security.request_security import require_conversation_scope, require_task_scope
from app.vision import VisionError, VisionService


router = APIRouter(prefix="/api", tags=["vision"])


@router.post("/vision/analyze")
async def analyze_vision(
    payload: VisionRequest,
    x_vision_model_api_key: str | None = Header(default=None),
) -> dict:
    scope = require_conversation_scope(
        payload.conversation_id,
        workspace=payload.workspace,
        permission_mode=payload.permission_mode,
    )
    require_task_scope(scope.conversation_id, payload.task_id)
    remote = payload.provider_mode == "remote"
    tool = "vision.remote" if remote else "vision.local"
    decision = authorize(
        mode=scope.permission_mode,
        risk="high" if remote else "low",
        tool=tool,
        arguments={
            "action": payload.action,
            "paths": payload.paths,
            "provider_mode": payload.provider_mode,
        },
        conversation_id=scope.conversation_id,
        task_id=payload.task_id,
        approval_tokens=payload.approval_tokens,
        approval_scope=payload.approval_scope,
        impact="清理元数据后的图片像素将发送到远程视觉 Provider" if remote else "读取当前工作区图片",
        workspace=scope.workspace,
    )
    if not decision.allowed:
        return decision.confirmation or {"status": "blocked", "error_code": "permission_denied"}
    try:
        return await VisionService().analyze(
            workspace=scope.workspace,
            paths=payload.paths,
            action=payload.action,
            prompt=payload.prompt,
            provider_mode=payload.provider_mode,
            api_key=x_vision_model_api_key,
            conversation_id=scope.conversation_id,
            task_id=payload.task_id,
        )
    except VisionError as exc:
        audit(scope.conversation_id, f"vision.{payload.action}", "", "error", {"error_code": exc.code})
        raise HTTPException(400, {"code": exc.code, "message": str(exc)}) from exc
