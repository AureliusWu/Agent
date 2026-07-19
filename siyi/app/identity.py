from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .database import audit, connect, now_iso, rows
from .identity_kernel import CANONICAL_IDENTITY


AGENT_ID = "natsume-kokoro-001"
ADMINISTRATOR_ID = "administrator-001"
_IDENTITY_PATH = Path(__file__).resolve().parents[2] / "kokoro" / "identity" / "natsume_kokoro.json"


def _canonical_identity() -> dict[str, Any]:
    payload = json.loads(_IDENTITY_PATH.read_text(encoding="utf-8")) if _IDENTITY_PATH.is_file() else json.loads(json.dumps(CANONICAL_IDENTITY, ensure_ascii=False))
    if payload.get("agent_id") != AGENT_ID or payload.get("user_id") != ADMINISTRATOR_ID:
        raise RuntimeError("Identity kernel identifiers do not match the application constants")
    return payload


def ensure_identity_kernel() -> None:
    payload = _canonical_identity()
    now = now_iso()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO agents(agent_id, display_name, active_identity_version, created_at, updated_at) "
            "VALUES(?,?,?,?,?)",
            (AGENT_ID, str(payload["display_name"]), int(payload["version"]), now, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO identity_versions(agent_id, version, payload, source, reason, actor_id, "
            "administrator_confirmed, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (AGENT_ID, int(payload["version"]), encoded, "bundled", "initial identity kernel", ADMINISTRATOR_ID, 1, now),
        )


def active_identity() -> dict[str, Any]:
    ensure_identity_kernel()
    records = rows(
        "SELECT a.agent_id, a.display_name, a.active_identity_version, v.payload, v.source, v.reason, v.created_at "
        "FROM agents a JOIN identity_versions v ON v.agent_id=a.agent_id AND v.version=a.active_identity_version "
        "WHERE a.agent_id=?",
        (AGENT_ID,),
    )
    if not records:
        raise RuntimeError("Active identity version is unavailable")
    record = records[0]
    record["identity"] = json.loads(record.pop("payload"))
    return record


def identity_versions() -> list[dict[str, Any]]:
    ensure_identity_kernel()
    items = rows(
        "SELECT agent_id, version, payload, source, reason, actor_id, administrator_confirmed, created_at "
        "FROM identity_versions WHERE agent_id=? ORDER BY version DESC",
        (AGENT_ID,),
    )
    for item in items:
        item["identity"] = json.loads(item.pop("payload"))
        item["administrator_confirmed"] = bool(item["administrator_confirmed"])
    return items


def create_identity_version(
    identity: dict[str, Any], *, reason: str, actor_id: str, administrator_confirmed: bool
) -> dict[str, Any]:
    if actor_id != ADMINISTRATOR_ID or not administrator_confirmed:
        raise PermissionError("Identity changes require explicit administrator confirmation")
    current = active_identity()
    payload = dict(identity)
    payload["agent_id"] = AGENT_ID
    payload["user_id"] = ADMINISTRATOR_ID
    if not str(payload.get("display_name") or "").strip():
        raise ValueError("display_name is required")
    next_version = int(current["active_identity_version"]) + 1
    payload["version"] = next_version
    now = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO identity_versions(agent_id, version, payload, source, reason, actor_id, "
            "administrator_confirmed, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (AGENT_ID, next_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), "administrator", reason, actor_id, 1, now),
        )
        db.execute(
            "UPDATE agents SET display_name=?, active_identity_version=?, updated_at=? WHERE agent_id=?",
            (str(payload["display_name"]), next_version, now, AGENT_ID),
        )
    audit(None, "identity_version_created", AGENT_ID, "ok", {"version": next_version, "reason": reason})
    return active_identity()


def activate_identity_version(version: int, *, actor_id: str, administrator_confirmed: bool) -> dict[str, Any]:
    if actor_id != ADMINISTRATOR_ID or not administrator_confirmed:
        raise PermissionError("Identity rollback requires explicit administrator confirmation")
    matches = rows("SELECT payload FROM identity_versions WHERE agent_id=? AND version=?", (AGENT_ID, version))
    if not matches:
        raise ValueError("Identity version does not exist")
    payload = json.loads(matches[0]["payload"])
    with connect() as db:
        db.execute(
            "UPDATE agents SET display_name=?, active_identity_version=?, updated_at=? WHERE agent_id=?",
            (str(payload["display_name"]), version, now_iso(), AGENT_ID),
        )
    audit(None, "identity_version_activated", AGENT_ID, "ok", {"version": version})
    return active_identity()


def identity_system_context() -> str:
    item = active_identity()
    identity = item["identity"]
    principles = "\n".join(f"- {value}" for value in identity.get("core_principles", []))
    rules = "\n".join(f"- {value}" for value in identity.get("response_rules", []))
    platform = "\n".join(f"- {value}" for value in CANONICAL_IDENTITY.get("platform_context", []))
    return (
        "[Identity Kernel - authoritative]\n"
        f"agent_id: {AGENT_ID}\n"
        f"identity_version: {item['active_identity_version']}\n"
        f"assistant_name: {identity['display_name']}\n"
        f"user_name: {identity['user_name']}\n"
        f"self_description: {identity['self_description']}\n"
        f"platform_context:\n{platform}\n"
        f"core_principles:\n{principles}\nresponse_rules:\n{rules}"
    )
