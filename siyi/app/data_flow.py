from __future__ import annotations

import json
from typing import Iterable

from .database import connect, now_iso


def record_data_flow(
    *,
    source: str,
    sink: str,
    classification: str,
    fields: Iterable[str] = (),
    redactions: int = 0,
    allowed: bool = True,
    reason: str = "",
    conversation_id: int | None = None,
    task_id: str | None = None,
) -> None:
    with connect() as db:
        if conversation_id is not None and not db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
            conversation_id = None
        if task_id is not None and not db.execute("SELECT 1 FROM agent_tasks WHERE id=?", (task_id,)).fetchone():
            task_id = None
        db.execute(
            "INSERT INTO data_flow_events(conversation_id, task_id, source, sink, classification, fields, redactions, allowed, reason, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, task_id, source, sink, classification, json.dumps(sorted(set(fields)), ensure_ascii=False), max(0, redactions), int(allowed), reason[:1000], now_iso()),
        )
