from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from .database import connect, now_iso, rows
from .task_events import emit_task_event


@dataclass(frozen=True)
class SegmentSnapshot:
    phase: str
    completed_steps: tuple[str, ...] = ()
    pending_steps: tuple[str, ...] = ()
    files_modified: tuple[str, ...] = ()
    tool_result_refs: tuple[str, ...] = ()
    context_summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0


def start_segment(task_id: str, reason: str, snapshot: SegmentSnapshot) -> dict[str, Any]:
    latest = rows("SELECT COALESCE(MAX(sequence),0) AS sequence FROM execution_segments WHERE task_id=?", (task_id,))[0]
    sequence = int(latest["sequence"]) + 1
    segment_id = f"seg_{uuid.uuid4().hex}"
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "UPDATE execution_segments SET status='interrupted',reason='process_restart',finished_at=? "
            "WHERE task_id=? AND status='running'",
            (stamp, task_id),
        )
        db.execute(
            "INSERT INTO execution_segments(id,task_id,sequence,status,reason,phase,completed_steps,pending_steps,"
            "files_modified,tool_result_refs,context_summary,input_tokens,output_tokens,total_tokens,model_calls,tool_calls,started_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                segment_id, task_id, sequence, "running", reason, snapshot.phase,
                json.dumps(snapshot.completed_steps, ensure_ascii=False),
                json.dumps(snapshot.pending_steps, ensure_ascii=False),
                json.dumps(snapshot.files_modified, ensure_ascii=False),
                json.dumps(snapshot.tool_result_refs, ensure_ascii=False),
                snapshot.context_summary, snapshot.input_tokens, snapshot.output_tokens,
                snapshot.total_tokens, snapshot.model_calls, snapshot.tool_calls, stamp,
            ),
        )
    emit_task_event(task_id, "execution.segment.started", {"id": segment_id, "sequence": sequence, "reason": reason, "phase": snapshot.phase})
    return {"id": segment_id, "sequence": sequence, "started_at": stamp}


def finish_segment(segment_id: str, task_id: str, status: str, reason: str, snapshot: SegmentSnapshot) -> None:
    with connect() as db:
        db.execute(
            "UPDATE execution_segments SET status=?,reason=?,phase=?,completed_steps=?,pending_steps=?,files_modified=?,"
            "tool_result_refs=?,context_summary=?,input_tokens=?,output_tokens=?,total_tokens=?,model_calls=?,tool_calls=?,finished_at=? WHERE id=?",
            (
                status, reason, snapshot.phase,
                json.dumps(snapshot.completed_steps, ensure_ascii=False),
                json.dumps(snapshot.pending_steps, ensure_ascii=False),
                json.dumps(snapshot.files_modified, ensure_ascii=False),
                json.dumps(snapshot.tool_result_refs, ensure_ascii=False), snapshot.context_summary,
                snapshot.input_tokens, snapshot.output_tokens, snapshot.total_tokens,
                snapshot.model_calls, snapshot.tool_calls, now_iso(), segment_id,
            ),
        )
    emit_task_event(task_id, "execution.segment.completed", {"id": segment_id, "status": status, "reason": reason})


def list_segments(task_id: str) -> list[dict[str, Any]]:
    items = rows("SELECT * FROM execution_segments WHERE task_id=? ORDER BY sequence", (task_id,))
    for item in items:
        for key in ("completed_steps", "pending_steps", "files_modified", "tool_result_refs"):
            item[key] = json.loads(item.get(key) or "[]")
    return items
