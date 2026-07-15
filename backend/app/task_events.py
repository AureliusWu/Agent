from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any

from .database import connect, now_iso, rows
from .trust import REDACTED, redact_payload


TERMINAL_EVENT_TYPES = {
    "task.completed",
    "task.failed",
    "task.cancelled",
    "task.paused",
}
_transient_payloads: OrderedDict[int, dict[str, Any]] = OrderedDict()


def _without_approval_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): REDACTED if str(key).lower() == "approval_key" else _without_approval_tokens(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_approval_tokens(item) for item in value]
    return value


def _contains_approval_token(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() == "approval_key" or _contains_approval_token(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_approval_token(item) for item in value)
    return False


def emit_task_event(task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_payload = payload or {}
    redacted, _ = redact_payload(raw_payload)
    cleaned = _without_approval_tokens(redacted)
    created_at = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO task_events(task_id, event_type, payload, created_at) VALUES(?,?,?,?)",
            (task_id, event_type, json.dumps(cleaned, ensure_ascii=False), created_at),
        )
        event_id = int(cursor.lastrowid)
        if event_type in TERMINAL_EVENT_TYPES:
            db.execute(
                "DELETE FROM task_events WHERE task_id=? AND id NOT IN "
                "(SELECT id FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 5000)",
                (task_id, task_id),
            )
    if _contains_approval_token(raw_payload):
        _transient_payloads[event_id] = raw_payload
        while len(_transient_payloads) > 1000:
            _transient_payloads.popitem(last=False)
    return {"id": event_id, "task_id": task_id, "event": event_type, "payload": raw_payload, "created_at": created_at}


def task_events(task_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    events = rows(
        "SELECT id, task_id, event_type, payload, created_at FROM task_events "
        "WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
        (task_id, max(0, after_id), min(max(limit, 1), 2000)),
    )
    for event in events:
        try:
            event["payload"] = json.loads(event.pop("payload") or "{}")
        except (TypeError, ValueError):
            event["payload"] = {}
        event["event"] = event.pop("event_type")
        if int(event["id"]) in _transient_payloads:
            event["payload"] = _transient_payloads[int(event["id"])]
    return events


def latest_terminal_event(task_id: str) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in TERMINAL_EVENT_TYPES)
    records = rows(
        f"SELECT id, task_id, event_type, payload, created_at FROM task_events "
        f"WHERE task_id=? AND event_type IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (task_id, *sorted(TERMINAL_EVENT_TYPES)),
    )
    if not records:
        return None
    event = records[0]
    event["event"] = event.pop("event_type")
    try:
        event["payload"] = json.loads(event.pop("payload") or "{}")
    except (TypeError, ValueError):
        event["payload"] = {}
    return event
