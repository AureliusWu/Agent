import io
import json
import re
import zipfile

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from ..database import audit
from ..memory import (
    delete_workspace_memory,
    list_workspace_memories,
    memory_feedback,
    update_workspace_memory,
    upsert_workspace_memory,
)
from ..schemas import MemoryCreate, MemoryFeedback, MemoryUpdate


router = APIRouter(prefix="/api/memories", tags=["memories"])
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_FILES = 100


@router.get("")
def list_memories(workspace: str, kind: str | None = None, namespace: str = "project", category: str | None = None, include_rejected: bool = True) -> list[dict]:
    try:
        return list_workspace_memories(workspace, kind, namespace=namespace, category=category, include_rejected=include_rejected)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("")
def create_memory(workspace: str, payload: MemoryCreate) -> dict:
    try:
        result = upsert_workspace_memory(workspace, source="user", verified=True, **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "create_memory", result["key"], "ok", {"workspace": workspace, "kind": result["kind"]})
    return result


@router.patch("/{memory_id}")
def update_memory(memory_id: int, workspace: str, payload: MemoryUpdate) -> dict:
    try:
        result = update_workspace_memory(workspace, memory_id, payload.model_dump(exclude_unset=True))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "update_memory", str(memory_id), "ok", {"workspace": workspace})
    return result


@router.delete("/{memory_id}")
def delete_memory(memory_id: int, workspace: str) -> dict:
    if not delete_workspace_memory(workspace, memory_id):
        raise HTTPException(404, "记忆不存在")
    audit(None, "delete_memory", str(memory_id), "ok", {"workspace": workspace})
    return {"id": memory_id, "deleted": True}


@router.post("/{memory_id}/feedback")
def feedback(memory_id: int, workspace: str, payload: MemoryFeedback) -> dict:
    try:
        result = memory_feedback(workspace, memory_id, payload.outcome)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "memory_feedback", str(memory_id), "ok", {"workspace": workspace, "outcome": payload.outcome})
    return result


def _text_memories(text: str, stem: str) -> list[dict]:
    sections = [part.strip() for part in re.split(r"(?m)^#{1,3}\s+", text) if part.strip()]
    return [
        {"key": f"import.{stem}.{index}", "content": section[:4000], "namespace": "personal", "category": "decision", "tags": ["imported"]}
        for index, section in enumerate(sections[:MAX_IMPORT_FILES], 1)
    ]


@router.post("/import")
async def import_memories(file: UploadFile = File(...), workspace: str = Form(default=""), namespace: str = Form(default="personal")) -> dict:
    if namespace not in {"personal", "project"}:
        raise HTTPException(400, "记忆命名空间必须是 personal 或 project")
    if namespace == "project" and not workspace.strip():
        raise HTTPException(400, "导入项目记忆前请先选择项目")
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "记忆导入文件不能超过 5 MB")
    name = (file.filename or "memory.txt").lower()
    candidates: list[dict] = []
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                if len(members) > MAX_IMPORT_FILES:
                    raise HTTPException(400, "压缩包文件数过多")
                for item in members:
                    if item.file_size > MAX_IMPORT_BYTES or ".." in item.filename.replace("\\", "/").split("/"):
                        raise HTTPException(400, "压缩包包含不安全路径或过大文件")
                    suffix = item.filename.lower()
                    if suffix.endswith((".md", ".txt")):
                        candidates.extend(_text_memories(archive.read(item).decode("utf-8", errors="replace"), re.sub(r"\W+", "-", item.filename)[:30]))
                    elif suffix.endswith(".json"):
                        payload = json.loads(archive.read(item).decode("utf-8"))
                        candidates.extend(payload.get("memories", []) if isinstance(payload, dict) else payload)
        elif name.endswith(".json"):
            payload = json.loads(raw.decode("utf-8"))
            candidates = payload.get("memories", []) if isinstance(payload, dict) else payload
        elif name.endswith((".md", ".txt")):
            candidates = _text_memories(raw.decode("utf-8", errors="replace"), re.sub(r"\W+", "-", name.rsplit(".", 1)[0])[:30])
        else:
            raise HTTPException(400, "仅支持 .zip、.json、.md 和 .txt")
    except (ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise HTTPException(400, "记忆文件无法解析") from exc
    imported = 0
    for index, item in enumerate(candidates[:MAX_IMPORT_FILES], 1):
        if not isinstance(item, dict) or not str(item.get("content") or "").strip():
            continue
        key = str(item.get("key") or f"import.memory.{index}")[:80]
        key = re.sub(r"[^\w.:-]", "-", key) or f"import.memory.{index}"
        raw_tags = item.get("tags") or ["imported"]
        tags = raw_tags if isinstance(raw_tags, list) else [str(raw_tags)]
        try:
            upsert_workspace_memory(
                workspace,
                key=key,
                content=str(item["content"])[:4000],
                namespace=namespace,
                category=str(item.get("category") or "decision"),
                tags=tags,
                source="user",
                verified=True,
            )
        except ValueError as exc:
            raise HTTPException(400, f"第 {index} 条记忆无效：{exc}") from exc
        imported += 1
    audit(None, "import_memories", name, "ok", {"count": imported, "namespace": namespace})
    return {"imported": imported}


@router.get("/export")
def export_memories(workspace: str = "", namespace: str = "personal") -> Response:
    try:
        items = list_workspace_memories(workspace, namespace=namespace)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = {"format": "siyi-memory-v1", "namespace": namespace, "memories": items}
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(body, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="siyi-{namespace}-memories.json"'})
