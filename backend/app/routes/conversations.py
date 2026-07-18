from fastapi import APIRouter, Header, HTTPException

from ..agent_profiles import require_agent_profile
from ..context import compact_conversation, context_stats
from ..context_assembler import context_debug
from ..database import audit, connect, now_iso, rows
from ..sandbox import workspace_root
from ..schemas import AgentProfileUpdate, CompactRequest, ConversationCreate, ConversationRename, PermissionUpdate

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("")
def conversations() -> list[dict]:
    return rows("SELECT * FROM conversations ORDER BY updated_at DESC")


@router.post("")
def create_conversation(payload: ConversationCreate) -> dict:
    root = str(workspace_root(payload.workspace)) if payload.workspace.strip() else ""
    now = now_iso()
    try:
        profile = require_agent_profile(payload.agent_profile_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    permission_mode = payload.permission_mode or (profile.default_permission if profile.source == "builtin" else "ask")
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title, workspace, permission_mode, agent_profile_id, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (payload.title, root, permission_mode, profile.id, now, now),
        )
    return {
        "id": cursor.lastrowid,
        "title": payload.title,
        "workspace": root,
        "permission_mode": permission_mode,
        "agent_profile_id": profile.id,
        "created_at": now,
        "updated_at": now,
    }


@router.patch("/{conversation_id}")
def rename_conversation(conversation_id: int, payload: ConversationRename) -> dict:
    with connect() as db:
        cursor = db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?", (payload.title.strip(), now_iso(), conversation_id))
        if not cursor.rowcount:
            raise HTTPException(404, "对话不存在")
    return {"id": conversation_id, "title": payload.title.strip()}


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: int) -> dict:
    with connect() as db:
        active = db.execute(
            "SELECT id,status FROM agent_tasks WHERE conversation_id=? AND status IN "
            "('pending','running','waiting_confirmation','paused','interrupted') LIMIT 1",
            (conversation_id,),
        ).fetchone()
        queued = db.execute(
            "SELECT id FROM conversation_queue_items WHERE conversation_id=? AND status IN ('pending','claimed') LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if active or queued:
            raise HTTPException(409, "对话仍有活动或可恢复任务，请先停止、取消或放弃恢复")
        cursor = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        if not cursor.rowcount:
            raise HTTPException(404, "对话不存在")
    return {"deleted": True, "id": conversation_id}


@router.get("/{conversation_id}/messages")
def messages(conversation_id: int) -> list[dict]:
    items = rows("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,))
    for item in items:
        item["reasoning"] = item.pop("reasoning_content", None)
    return items


@router.delete("/{conversation_id}/messages")
def clear_messages(conversation_id: int) -> dict:
    with connect() as db:
        active = db.execute(
            "SELECT 1 FROM agent_tasks WHERE conversation_id=? AND status IN "
            "('pending','running','waiting_confirmation','paused','interrupted') LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if active:
            raise HTTPException(409, "对话仍有活动或可恢复任务，不能清空")
        deleted = db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,)).rowcount
        db.execute("DELETE FROM conversation_context WHERE conversation_id=?", (conversation_id,))
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), conversation_id))
    audit(conversation_id, "clear_messages", "conversation", "ok", {"deleted": deleted})
    return {"cleared": True, "deleted": deleted}


@router.get("/{conversation_id}/context")
def conversation_context(conversation_id: int) -> dict:
    return context_stats(conversation_id)


@router.get("/{conversation_id}/context-debug")
def conversation_context_debug(conversation_id: int) -> dict:
    return context_debug(conversation_id)


@router.post("/{conversation_id}/compact")
async def compact(conversation_id: int, payload: CompactRequest, x_model_api_key: str | None = Header(default=None)) -> dict:
    try:
        result = await compact_conversation(conversation_id, x_model_api_key, force=payload.force)
        audit(conversation_id, "compact_context", "conversation", "ok", result)
        return result
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/{conversation_id}/runs")
def tool_runs(conversation_id: int) -> list[dict]:
    return rows("SELECT * FROM tool_runs WHERE conversation_id=? ORDER BY id DESC LIMIT 100", (conversation_id,))


@router.get("/{conversation_id}/model-runs")
def model_runs(conversation_id: int) -> list[dict]:
    return rows("SELECT * FROM model_runs WHERE conversation_id=? ORDER BY id DESC LIMIT 100", (conversation_id,))


@router.get("/{conversation_id}/tasks")
def tasks(conversation_id: int) -> list[dict]:
    return rows("SELECT * FROM agent_tasks WHERE conversation_id=? ORDER BY created_at DESC LIMIT 100", (conversation_id,))


@router.patch("/{conversation_id}/permission")
def update_permission(conversation_id: int, payload: PermissionUpdate) -> dict:
    with connect() as db:
        cursor = db.execute("UPDATE conversations SET permission_mode=?, updated_at=? WHERE id=?", (payload.permission_mode, now_iso(), conversation_id))
        if not cursor.rowcount:
            raise HTTPException(404, "对话不存在")
    audit(conversation_id, "permission_mode", payload.permission_mode, "ok")
    return {"id": conversation_id, "permission_mode": payload.permission_mode}


@router.patch("/{conversation_id}/profile")
def update_agent_profile(conversation_id: int, payload: AgentProfileUpdate) -> dict:
    try:
        profile = require_agent_profile(payload.agent_profile_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with connect() as db:
        active = db.execute(
            "SELECT id FROM agent_tasks WHERE conversation_id=? AND status IN ('pending','running','waiting_confirmation') LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if active:
            raise HTTPException(409, "任务运行或等待确认期间不能切换 Agent Profile")
        cursor = db.execute(
            "UPDATE conversations SET agent_profile_id=?, updated_at=? WHERE id=?",
            (profile.id, now_iso(), conversation_id),
        )
        if not cursor.rowcount:
            raise HTTPException(404, "对话不存在")
    audit(conversation_id, "agent_profile", profile.id, "ok", {"source": profile.source})
    return {"id": conversation_id, "agent_profile_id": profile.id}
