from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .config import settings
from .database import connect, now_iso


def _artifact_root() -> Path:
    root = Path(settings.database_path).resolve().parent / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def store_artifact(
    content: str | bytes,
    *,
    task_id: str,
    tool_call_id: str,
    media_type: str = "text/plain; charset=utf-8",
) -> dict[str, Any]:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    artifact_id = uuid.uuid4().hex
    path = _artifact_root() / f"{artifact_id}.bin"
    path.write_bytes(raw)
    with connect() as db:
        db.execute(
            "INSERT INTO task_artifacts(id,task_id,tool_call_id,media_type,path,total_bytes,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (artifact_id, task_id, tool_call_id, media_type, str(path), len(raw), now_iso()),
        )
    return {"artifact_id": artifact_id, "total_bytes": len(raw), "media_type": media_type}


def store_json_artifact(value: Any, *, task_id: str, tool_call_id: str) -> dict[str, Any]:
    return store_artifact(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        task_id=task_id,
        tool_call_id=tool_call_id,
        media_type="application/json; charset=utf-8",
    )


def read_artifact(artifact_id: str, *, offset: int = 0, limit: int = 40_000) -> dict[str, Any]:
    with connect() as db:
        record = db.execute("SELECT * FROM task_artifacts WHERE id=?", (artifact_id,)).fetchone()
    if record is None:
        raise KeyError(artifact_id)
    raw = Path(str(record["path"])).read_bytes()
    start = min(max(0, offset), len(raw))
    end = min(start + max(1, limit), len(raw))
    return {
        "artifact_id": artifact_id,
        "offset": start,
        "next_offset": end,
        "total_bytes": len(raw),
        "content": raw[start:end].decode("utf-8", errors="replace"),
        "truncated": end < len(raw),
        "media_type": str(record["media_type"]),
    }
