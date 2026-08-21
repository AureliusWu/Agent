from __future__ import annotations

import json
from typing import Any

from app.database import connect, now_iso, rows


_SAFE_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "MIC_PERMISSION": frozenset({"status"}),
    "RECORDING_STARTED": frozenset({"device_id"}),
    # Meter samples are intentionally rounded, metadata-only, and emitted at
    # a bounded cadence by VoiceSessionManager.  They are useful for the
    # event timeline without ever persisting waveform data.
    "RECORDING_LEVEL": frozenset({"level"}),
    "RECORDING_STOPPED": frozenset({"duration_ms"}),
    "AUDIO_READY": frozenset({"duration_ms", "sample_rate"}),
    "STT_LOADING": frozenset({"provider", "model"}),
    "STT_STARTED": frozenset({"provider", "model"}),
    "STT_PARTIAL": frozenset({"provider", "model", "duration_ms", "text_length"}),
    "STT_COMPLETED": frozenset({"provider", "model", "duration_ms", "text_length"}),
    "STT_CANCELLED": frozenset({"reason"}),
    "STT_FAILED": frozenset({"code"}),
    "MESSAGE_READY": frozenset({"text_length", "auto_send"}),
    "MESSAGE_SENT": frozenset({"task_id", "message_id"}),
    "VOICE_INPUT_QUEUED": frozenset({"task_id", "message_id"}),
    "AGENT_STARTED": frozenset({"task_id"}),
    "AGENT_COMPLETED": frozenset({"task_id", "status"}),
    "AGENT_FAILED": frozenset({"task_id", "status"}),
    "AGENT_CANCELLED": frozenset({"task_id", "status"}),
    "TTS_STARTED": frozenset({"task_id", "request_id"}),
    "TTS_STOPPED": frozenset({"task_id", "request_id"}),
    "TTS_COMPLETED": frozenset({"task_id", "request_id"}),
    "VOICE_SESSION_COMPLETED": frozenset({"status"}),
}

def _metadata_only_payload(event: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Keep event persistence on a narrow, reviewed metadata schema.

    A blacklist is too easy to bypass with nested keys or a future field such
    as ``transcript_text``.  Voice SSE needs only status and correlation
    metadata, so unknown fields and nested values are intentionally discarded.
    """
    allowed = _SAFE_EVENT_FIELDS.get(event, frozenset())
    cleaned: dict[str, Any] = {}
    for key in allowed:
        value = (payload or {}).get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                cleaned[key] = value
    return cleaned


def emit_voice_event(voice_session_id: str, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist only metadata.  Transcript text and audio are intentionally excluded."""
    cleaned = _metadata_only_payload(event, payload)
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO voice_event_records(voice_session_id,event,payload_json,created_at) VALUES(?,?,?,?)",
            (voice_session_id, event, json.dumps(cleaned, ensure_ascii=True, sort_keys=True), stamp),
        )
    return {"id": int(cursor.lastrowid), "voice_session_id": voice_session_id, "event": event, "payload": cleaned, "created_at": stamp}


def voice_events(voice_session_id: str, after_id: int = 0) -> list[dict[str, Any]]:
    result = []
    for record in rows("SELECT id,voice_session_id,event,payload_json,created_at FROM voice_event_records WHERE voice_session_id=? AND id>? ORDER BY id", (voice_session_id, after_id)):
        result.append({**record, "payload": json.loads(record.pop("payload_json") or "{}")})
    return result
