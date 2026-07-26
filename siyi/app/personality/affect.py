from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from app.database import audit, connect, now_iso, rows
from app.personality.identity_service import ADMINISTRATOR_ID, AGENT_ID


DEFAULT_TRAIT = {"calm": 0.82, "warmth": 0.74, "reserve": 0.77, "curiosity": 0.84, "sensitivity": 0.65}
DEFAULT_MOOD = {"valence": 0.15, "energy": 0.52, "security": 0.78, "loneliness": 0.18, "confidence": 0.71}
DEFAULT_EMOTION = {"type": "calm", "intensity": 0.0, "trigger_memory_id": None, "started_at": None, "decay_rate": 0.12}
DEFAULT_RELATIONSHIP = {
    "trust": 0.5,
    "familiarity": 0.1,
    "emotional_closeness": 0.1,
    "collaboration_depth": 0.1,
    "felt_safety": 0.5,
    "recent_tension": 0.0,
    "gratitude": 0.0,
    "concern": 0.0,
}
RELATIONSHIP_FIELDS = set(DEFAULT_RELATIONSHIP)
MAX_DELTAS = {"normal": 0.01, "important": 0.05, "manual": 1.0}


def _decode(value: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return dict(fallback)
    return parsed if isinstance(parsed, dict) else dict(fallback)


def ensure_affect_state() -> None:
    now = now_iso()
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO affect_states(agent_id,trait_json,mood_json,emotion_json,updated_at) VALUES(?,?,?,?,?)",
            (AGENT_ID, json.dumps(DEFAULT_TRAIT), json.dumps(DEFAULT_MOOD), json.dumps(DEFAULT_EMOTION), now),
        )
        db.execute(
            "INSERT OR IGNORE INTO relationship_states(agent_id,user_id,state_json,shared_history_count,updated_at) VALUES(?,?,?,?,?)",
            (AGENT_ID, ADMINISTRATOR_ID, json.dumps(DEFAULT_RELATIONSHIP), 0, now),
        )


def current_affect(*, apply_decay: bool = True) -> dict[str, Any]:
    ensure_affect_state()
    item = rows("SELECT * FROM affect_states WHERE agent_id=?", (AGENT_ID,))[0]
    trait = _decode(item["trait_json"], DEFAULT_TRAIT)
    mood = _decode(item["mood_json"], DEFAULT_MOOD)
    emotion = _decode(item["emotion_json"], DEFAULT_EMOTION)
    if apply_decay and emotion.get("started_at") and float(emotion.get("intensity") or 0) > 0:
        try:
            elapsed_hours = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(str(emotion["started_at"]))).total_seconds() / 3600)
        except ValueError:
            elapsed_hours = 0.0
        decay_rate = max(0.0, min(1.0, float(emotion.get("decay_rate") or 0.12)))
        decayed = float(emotion["intensity"]) * math.exp(-decay_rate * elapsed_hours)
        if abs(decayed - float(emotion["intensity"])) > 0.001:
            emotion["intensity"] = round(max(0.0, decayed), 4)
            with connect() as db:
                db.execute("UPDATE affect_states SET emotion_json=?,updated_at=? WHERE agent_id=?", (json.dumps(emotion), now_iso(), AGENT_ID))
    return {"agent_id": AGENT_ID, "trait": trait, "mood": mood, "emotion": emotion, "updated_at": item["updated_at"]}


def current_relationship() -> dict[str, Any]:
    ensure_affect_state()
    item = rows("SELECT * FROM relationship_states WHERE agent_id=? AND user_id=?", (AGENT_ID, ADMINISTRATOR_ID))[0]
    return {
        "agent_id": AGENT_ID,
        "user_id": ADMINISTRATOR_ID,
        "state": _decode(item["state_json"], DEFAULT_RELATIONSHIP),
        "shared_history_count": int(item["shared_history_count"]),
        "last_meaningful_event_id": item["last_meaningful_event_id"],
        "updated_at": item["updated_at"],
    }


def record_affect_event(
    event_type: str,
    *,
    intensity: float,
    valence_delta: float = 0.0,
    trigger_memory_id: str | None = None,
    decay_rate: float = 0.12,
    conversation_id: int | None = None,
) -> dict[str, Any]:
    state = current_affect()
    now = now_iso()
    emotion = {
        "type": event_type[:50],
        "intensity": round(max(0.0, min(1.0, intensity)), 4),
        "trigger_memory_id": trigger_memory_id,
        "started_at": now,
        "decay_rate": max(0.0, min(1.0, decay_rate)),
    }
    mood = dict(state["mood"])
    mood["valence"] = round(max(-1.0, min(1.0, float(mood.get("valence") or 0) + max(-0.05, min(0.05, valence_delta)))), 4)
    event_id = f"aff_{uuid.uuid4().hex}"
    with connect() as db:
        db.execute(
            "UPDATE affect_states SET mood_json=?,emotion_json=?,updated_at=? WHERE agent_id=?",
            (json.dumps(mood), json.dumps(emotion), now, AGENT_ID),
        )
        db.execute(
            "INSERT INTO affect_events(id,agent_id,event_type,payload_json,source_conversation_id,created_at) VALUES(?,?,?,?,?,?)",
            (event_id, AGENT_ID, event_type, json.dumps({"intensity": intensity, "valence_delta": valence_delta, "trigger_memory_id": trigger_memory_id}), str(conversation_id) if conversation_id else None, now),
        )
    audit(conversation_id, "affect_event", event_id, "ok", {"event_type": event_type, "intensity": emotion["intensity"]})
    return current_affect(apply_decay=False)


def update_relationship(
    delta: dict[str, float], *, importance: str = "normal", reason: str,
    conversation_id: int | None = None,
) -> dict[str, Any]:
    if importance not in MAX_DELTAS:
        raise ValueError("Unsupported relationship event importance")
    current = current_relationship()
    state = dict(current["state"])
    limit = MAX_DELTAS[importance]
    applied: dict[str, float] = {}
    for key, raw_value in delta.items():
        if key not in RELATIONSHIP_FIELDS:
            continue
        bounded = max(-limit, min(limit, float(raw_value)))
        state[key] = round(max(0.0, min(1.0, float(state.get(key) or 0) + bounded)), 4)
        applied[key] = bounded
    event_id = f"rel_{uuid.uuid4().hex}"
    now = now_iso()
    meaningful = importance in {"important", "manual"} and bool(applied)
    with connect() as db:
        db.execute(
            "UPDATE relationship_states SET state_json=?,shared_history_count=shared_history_count+1,"
            "last_meaningful_event_id=CASE WHEN ? THEN ? ELSE last_meaningful_event_id END,updated_at=? "
            "WHERE agent_id=? AND user_id=?",
            (json.dumps(state), int(meaningful), event_id, now, AGENT_ID, ADMINISTRATOR_ID),
        )
        db.execute(
            "INSERT INTO relationship_events(id,agent_id,user_id,importance,delta_json,reason,source_conversation_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (event_id, AGENT_ID, ADMINISTRATOR_ID, importance, json.dumps(applied), reason[:1000], str(conversation_id) if conversation_id else None, now),
        )
    audit(conversation_id, "relationship_event", event_id, "ok", {"importance": importance, "fields": sorted(applied)})
    return current_relationship()


def record_completed_interaction(conversation_id: int) -> None:
    update_relationship(
        {"familiarity": 0.001, "collaboration_depth": 0.001},
        importance="normal",
        reason="completed interaction",
        conversation_id=conversation_id,
    )


def affect_system_context() -> str:
    affect = current_affect()
    relationship = current_relationship()
    return (
        "[Affect and relationship state - style guidance only]\n"
        f"mood={json.dumps(affect['mood'], ensure_ascii=False)}\n"
        f"emotion={json.dumps(affect['emotion'], ensure_ascii=False)}\n"
        f"relationship={json.dumps(relationship['state'], ensure_ascii=False)}\n"
        "These values may influence tone and attentiveness only. They must never alter facts, safety, permissions, or identity."
    )
