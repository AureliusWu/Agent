from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from app.database import connect, now_iso, rows
from app.security.trust import redact_payload


def record_performance_trace(
    span_name: str,
    component: str,
    duration_ms: float,
    *,
    task_id: str | None = None,
    status: str = "ok",
    metadata: dict[str, Any] | None = None,
    started_at: str | None = None,
) -> str:
    trace_id = uuid.uuid4().hex
    cleaned, _ = redact_payload(metadata or {})
    finished_at = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO performance_traces(id,task_id,span_name,component,duration_ms,status,metadata,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (trace_id, task_id, span_name, component, round(max(0.0, duration_ms), 3), status, json.dumps(cleaned, ensure_ascii=False), started_at or finished_at, finished_at),
        )
    return trace_id


@contextmanager
def performance_span(span_name: str, component: str, *, task_id: str | None = None, metadata: dict[str, Any] | None = None) -> Iterator[None]:
    started_at = now_iso()
    started = time.perf_counter()
    status = "ok"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        record_performance_trace(span_name, component, (time.perf_counter() - started) * 1000, task_id=task_id, status=status, metadata=metadata, started_at=started_at)


def performance_summary(task_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    where = "WHERE task_id=?" if task_id else ""
    params = (task_id, min(max(limit, 1), 1000)) if task_id else (min(max(limit, 1), 1000),)
    traces = rows(f"SELECT span_name,component,duration_ms,status,metadata,started_at,finished_at FROM performance_traces {where} ORDER BY started_at DESC LIMIT ?", params)
    for trace in traces:
        trace["metadata"] = json.loads(trace["metadata"] or "{}")
    aggregates: dict[str, dict[str, float | int]] = {}
    for trace in traces:
        bucket = aggregates.setdefault(trace["span_name"], {"samples": 0, "total_ms": 0.0, "max_ms": 0.0})
        bucket["samples"] = int(bucket["samples"]) + 1
        bucket["total_ms"] = float(bucket["total_ms"]) + float(trace["duration_ms"])
        bucket["max_ms"] = max(float(bucket["max_ms"]), float(trace["duration_ms"]))
    for bucket in aggregates.values():
        bucket["average_ms"] = round(float(bucket["total_ms"]) / int(bucket["samples"]), 3)
    return {"traces": traces, "aggregates": aggregates}
