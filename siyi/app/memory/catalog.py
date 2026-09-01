from __future__ import annotations

import json
from typing import Any

from app.database import rows
from app.personality.identity_service import ADMINISTRATOR_ID, AGENT_ID


_SOURCE_PRIORITY = {
    "agent_inference": 1,
    "system_observation": 2,
    "imported_document": 3,
    "manual_entry": 4,
    "conversation": 5,
    "user_confirmed": 6,
}

_STATUS_PRIORITY = {
    "deleted": 0,
    "rejected": 1,
    "expired": 2,
    "archived": 3,
    "superseded": 4,
    "candidate": 5,
    "active": 6,
}


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _memory_type(record: dict[str, Any]) -> str:
    explicit = str(record.get("memory_type") or "")
    if explicit in {"semantic", "episodic", "procedural", "relationship"}:
        return explicit
    if str(record.get("kind") or "") == "experience":
        return "episodic"
    if str(record.get("category") or "") in {"user_constraint", "coding_convention", "build_command", "test_command"}:
        return "procedural"
    return "semantic"


def _public(record: dict[str, Any]) -> dict[str, Any]:
    metadata = _json_object(record.get("metadata_json"))
    provenance = _json_object(record.get("source_metadata_json"))
    try:
        tags_value = json.loads(str(record.get("tags") or "[]"))
    except (TypeError, ValueError):
        tags_value = []
    tags = [str(item) for item in tags_value] if isinstance(tags_value, list) else []
    status = str(record.get("status") or "active")
    if bool(record.get("rejected")) and status == "active":
        status = "rejected"
    owner_api = "long_term" if record.get("legacy_table") == "memories" else "scoped"
    return {
        "id": str(record["record_id"]),
        "record_row_id": int(record["id"]),
        "agent_id": str(record.get("agent_id") or AGENT_ID),
        "user_id": str(record.get("scope_id") or ADMINISTRATOR_ID),
        "scope_type": "user",
        "scope_id": str(record.get("scope_id") or ADMINISTRATOR_ID),
        "memory_type": _memory_type(record),
        "key": str(record.get("key") or ""),
        "kind": str(record.get("kind") or "project"),
        "category": str(record.get("category") or "decision"),
        "title": record.get("title"),
        "content": str(record.get("content") or ""),
        "normalized_content": str(record.get("normalized_content") or ""),
        "content_fingerprint": str(record.get("content_fingerprint") or ""),
        "source_type": str(record.get("source_type") or "manual_entry"),
        "source_conversation_id": record.get("source_conversation_id"),
        "source_message_id": record.get("source_message_id"),
        "confidence": float(record.get("confidence") or 0),
        "importance": float(record.get("importance") or 0.5),
        "emotional_weight": float(record.get("emotional_weight") or 0),
        "access_count": int(record.get("access_count") or 0),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "occurred_at": record.get("occurred_at"),
        "valid_from": record.get("valid_from"),
        "valid_until": record.get("valid_until"),
        "last_accessed_at": record.get("last_accessed_at"),
        "status": status,
        "supersedes_memory_id": record.get("supersedes_record_id"),
        "user_confirmed": bool(record.get("user_confirmed")),
        "is_locked": bool(record.get("is_locked")),
        "is_sensitive": bool(record.get("is_sensitive")),
        "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        "metadata": metadata,
        "tags": tags,
        "applicable_version": record.get("applicable_version"),
        "project_signature": _json_object(record.get("project_signature")),
        "last_verified_at": record.get("last_verified_at"),
        "last_used_at": record.get("last_used_at"),
        "use_count": int(record.get("use_count") or 0),
        "success_count": int(record.get("success_count") or 0),
        "failure_count": int(record.get("failure_count") or 0),
        "rejected": bool(record.get("rejected")),
        "invalidated_reason": record.get("invalidated_reason"),
        "provenance": {
            "source_type": str(record.get("source_type") or "manual_entry"),
            "source_task_id": record.get("source_task_id"),
            "source_conversation_id": record.get("source_conversation_id"),
            "source_message_id": record.get("source_message_id"),
            "source_metadata": provenance,
            "legacy_table": record.get("legacy_table"),
            "legacy_id": record.get("legacy_id"),
        },
        "legacy_table": record.get("legacy_table"),
        "legacy_id": record.get("legacy_id"),
        "owner_api": owner_api,
        "editable_via_current_api": owner_api == "long_term",
        "read_only_compatibility": owner_api != "long_term",
        "dedup_aliases": [],
    }


def authoritative_user_memories(
    *, include_duplicates: bool = False, statuses: set[str] | None = None
) -> list[dict[str, Any]]:
    """Read the user scope from the v2 store and deterministically de-duplicate it."""

    records = [
        _public(item)
        for item in rows(
            "SELECT * FROM memory_records WHERE agent_id=? AND scope_type='user' AND scope_id=? "
            "ORDER BY updated_at DESC,id DESC",
            (AGENT_ID, ADMINISTRATOR_ID),
        )
    ]
    if statuses is not None:
        records = [item for item in records if item["status"] in statuses]
    if include_duplicates:
        return records
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        fingerprint = item["content_fingerprint"] or f"record:{item['id']}"
        grouped.setdefault(fingerprint, []).append(item)
    result: list[dict[str, Any]] = []
    for items in grouped.values():
        items.sort(
            key=lambda item: (
                _STATUS_PRIORITY.get(str(item.get("status") or ""), -1),
                item.get("legacy_table") == "memories",
                bool(item.get("user_confirmed")),
                bool(item.get("is_locked")),
                _SOURCE_PRIORITY.get(str(item.get("source_type") or ""), 0),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        winner = items[0]
        winner["dedup_aliases"] = [item["id"] for item in items[1:]]
        result.append(winner)
    result.sort(
        key=lambda item: (bool(item.get("is_locked")), float(item.get("importance") or 0), str(item.get("updated_at") or "")),
        reverse=True,
    )
    return result


def authoritative_user_memory(record_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
    # Point lookups preserve identity.  De-duplication belongs to list and
    # retrieval views; resolving an alias to a different winner would let a
    # caller accidentally mutate the winner through the wrong compatibility
    # API.
    for item in authoritative_user_memories(include_duplicates=True):
        if record_id == item["id"]:
            if item["status"] == "deleted" and not include_deleted:
                break
            return item
    raise KeyError("Memory does not exist")
