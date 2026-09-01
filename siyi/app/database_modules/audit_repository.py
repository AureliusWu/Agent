from __future__ import annotations

import json
from typing import Any

from app.security.trust import redact_payload


def sanitize_details(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("key", "token", "secret", "password", "authorization")):
                cleaned[key] = "***"
            elif key == "content" and isinstance(item, str):
                cleaned[key] = f"<content {len(item.encode('utf-8'))} bytes>"
            else:
                cleaned[key] = sanitize_details(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_details(item) for item in value[:100]]
    if isinstance(value, str):
        cleaned, _ = redact_payload(value)
        return cleaned[:2000] + "…" if len(cleaned) > 2000 else cleaned
    return value


def audit(
    conversation_id: int | None,
    action: str,
    target: str,
    status: str,
    details: Any = None,
) -> None:
    from app import database as facade

    cleaned, sensitive = redact_payload(details)
    with facade.connect() as db:
        db.execute(
            "INSERT INTO audit_logs(conversation_id, action, target, status, details, created_at) VALUES(?,?,?,?,?,?)",
            (
                conversation_id,
                action,
                target,
                status,
                json.dumps(sanitize_details(cleaned), ensure_ascii=False) if details is not None else None,
                facade.now_iso(),
            ),
        )
        if sensitive.redactions:
            flow_conversation = (
                conversation_id
                if conversation_id is not None
                and db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone()
                else None
            )
            db.execute(
                "INSERT INTO data_flow_events(conversation_id, source, sink, classification, fields, redactions, allowed, reason, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    flow_conversation,
                    "application_event",
                    "audit_log",
                    "credential",
                    '["details"]',
                    sensitive.redactions,
                    1,
                    "credentials redacted before logging",
                    facade.now_iso(),
                ),
            )
