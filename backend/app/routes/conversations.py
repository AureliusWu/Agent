from fastapi import APIRouter, Header, HTTPException

from ..context import compact_conversation, context_stats
from ..database import audit, connect, now_iso, rows
from ..sandbox import workspace_root
from ..schemas import CompactRequest, ConversationCreate, ConversationRename, PermissionUpdate

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("")
def conversations() -> list[dict]:
    return rows("SELECT * FROM conversations ORDER BY updated_at DESC")


@router.post("")
def create_conversation(payload: ConversationCreate) -> dict:
    root, now = workspace_root(payload.workspace), now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?)",
            (payload.title, str(root), payload.permission_mode, now, now),
        )
    return {"id": cursor.lastrowid, "title": payload.title, "workspace": str(root), "permission_mode": payload.permission_mode, "created_at": now, "updated_at": now}


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
        cursor = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        if not cursor.rowcount:
            raise HTTPException(404, "对话不存在")
    return {"deleted": True, "id": conversation_id}


@router.get("/{conversation_id}/messages")
def messages(conversation_id: int) -> list[dict]:
    return rows("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,))


@router.get("/{conversation_id}/context")
def conversation_context(conversation_id: int) -> dict:
    return context_stats(conversation_id)


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
