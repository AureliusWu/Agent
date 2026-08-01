from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.database import connect, now_iso


_MEDIA_EXTENSIONS = {
    "application/json": "json",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "image/png": "png",
    "text/markdown": "md",
    "text/plain": "txt",
}


def _artifact_root() -> Path:
    root = Path(settings.database_path).resolve().parent / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(value: str | None, media_type: str) -> str:
    candidate = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    candidate = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]+", "_", candidate)
    candidate = candidate.strip(" .")
    extension = _MEDIA_EXTENSIONS.get(
        media_type.split(";", 1)[0].strip().casefold(),
        "bin",
    )
    if not candidate:
        return f"artifact.{extension}"
    if extension != "bin" and Path(candidate).suffix.casefold() != f".{extension}":
        candidate = f"{Path(candidate).stem or 'artifact'}.{extension}"
    if len(candidate) > 120:
        suffix = Path(candidate).suffix[:16]
        stem_limit = max(1, 120 - len(suffix))
        candidate = f"{Path(candidate).stem[:stem_limit]}{suffix}"
    return candidate


def store_artifact(
    content: str | bytes,
    *,
    task_id: str,
    tool_call_id: str,
    media_type: str = "text/plain; charset=utf-8",
    filename: str | None = None,
) -> dict[str, Any]:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    content_sha256 = hashlib.sha256(raw).hexdigest()
    safe_filename = _safe_filename(filename, media_type)
    artifact_id = hashlib.sha256(
        f"{task_id}\0{content_sha256}\0{safe_filename}".encode("utf-8")
    ).hexdigest()
    path = _artifact_root() / f"{content_sha256}.bin"
    deduplicated = (
        path.is_file()
        and path.stat().st_size == len(raw)
        and hashlib.sha256(path.read_bytes()).hexdigest() == content_sha256
    )
    if not deduplicated:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{artifact_id}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO task_artifacts("
            "id,task_id,tool_call_id,media_type,filename,content_sha256,path,total_bytes,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                artifact_id,
                task_id,
                tool_call_id,
                media_type,
                safe_filename,
                content_sha256,
                str(path),
                len(raw),
                now_iso(),
            ),
        )
    return {
        "artifact_id": artifact_id,
        "content_sha256": content_sha256,
        "filename": safe_filename,
        "total_bytes": len(raw),
        "media_type": media_type,
        "deduplicated": deduplicated,
    }


def store_json_artifact(value: Any, *, task_id: str, tool_call_id: str) -> dict[str, Any]:
    return store_artifact(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        task_id=task_id,
        tool_call_id=tool_call_id,
        media_type="application/json; charset=utf-8",
        filename="artifact.json",
    )


def read_artifact(artifact_id: str, *, offset: int = 0, limit: int = 40_000) -> dict[str, Any]:
    record, raw = read_artifact_bytes(artifact_id)
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
        "filename": _safe_filename(record.get("filename"), str(record["media_type"])),
        "download_url": f"/api/artifacts/{artifact_id}/raw",
    }


def read_artifact_bytes(artifact_id: str) -> tuple[dict[str, Any], bytes]:
    with connect() as db:
        record = db.execute("SELECT * FROM task_artifacts WHERE id=?", (artifact_id,)).fetchone()
    if record is None:
        raise KeyError(artifact_id)
    record_data = dict(record)
    raw = Path(str(record_data["path"])).read_bytes()
    if len(raw) != int(record_data["total_bytes"]):
        raise OSError("Stored artifact size does not match its database record")
    expected_hash = str(record_data.get("content_sha256") or "")
    if expected_hash and hashlib.sha256(raw).hexdigest() != expected_hash:
        raise OSError("Stored artifact hash does not match its database record")
    return record_data, raw


def list_task_artifacts(task_id: str) -> list[dict[str, Any]]:
    with connect() as db:
        records = db.execute(
            "SELECT id,media_type,filename,total_bytes,content_sha256,created_at "
            "FROM task_artifacts WHERE task_id=? ORDER BY created_at,id",
            (task_id,),
        ).fetchall()
    return [
        {
            "artifact_id": str(record["id"]),
            "media_type": str(record["media_type"]),
            "filename": _safe_filename(
                str(record["filename"] or ""),
                str(record["media_type"]),
            ),
            "total_bytes": int(record["total_bytes"]),
            "content_sha256": str(record["content_sha256"] or ""),
            "created_at": str(record["created_at"]),
            "download_url": f"/api/artifacts/{record['id']}/raw",
        }
        for record in records
    ]
