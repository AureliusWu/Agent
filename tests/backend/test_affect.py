from datetime import datetime, timedelta, timezone
import json

from app.affect import current_affect, current_relationship, record_affect_event, update_relationship
from app.database import connect
from app.identity import AGENT_ID


def test_normal_relationship_delta_is_clamped() -> None:
    before = current_relationship()["state"]
    after = update_relationship({"trust": 0.9, "recent_tension": -0.9}, importance="normal", reason="clamp test")["state"]
    assert after["trust"] <= min(1, before["trust"] + 0.01)
    assert after["recent_tension"] >= max(0, before["recent_tension"] - 0.01)


def test_important_relationship_delta_allows_larger_but_bounded_change() -> None:
    before = current_relationship()["state"]["collaboration_depth"]
    after = update_relationship({"collaboration_depth": 1}, importance="important", reason="milestone")["state"]["collaboration_depth"]
    assert after == min(1, round(before + 0.05, 4))


def test_emotion_decays_over_elapsed_time_without_changing_trait() -> None:
    state = record_affect_event("concern", intensity=0.8, valence_delta=-0.02, decay_rate=0.2)
    trait = state["trait"]
    emotion = dict(state["emotion"])
    emotion["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    with connect() as db:
        db.execute("UPDATE affect_states SET emotion_json=? WHERE agent_id=?", (json.dumps(emotion), AGENT_ID))
    decayed = current_affect()
    assert decayed["emotion"]["intensity"] < 0.8
    assert decayed["trait"] == trait


def test_unknown_relationship_fields_are_ignored() -> None:
    state = update_relationship({"dependency": 1, "trust": 0.001}, importance="normal", reason="safe fields")
    assert "dependency" not in state["state"]
