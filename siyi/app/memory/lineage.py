"""Host-authored provenance for project memories, not a fact-verification claim."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.memory.model import MemoryScope
from app.sandbox import workspace_root


LINEAGE_KEY = "_siyi_source_lineage"
GENERATED_SOURCES = frozenset({"automatic", "agent"})


class MemorySourceError(ValueError):
    """A generated record lacks an eligible durable task origin."""


def _digest(parts: list[Any]) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def content_revision(content: str) -> str:
    # Unlike the deduplication fingerprint, case/whitespace changes matter.
    return _digest(["siyi-memory-content/v1", content])


def read_task_origins(db: sqlite3.Connection, task_ids: set[str], agent_id: str) -> dict[str, dict[str, Any]]:
    if not task_ids:
        return {}
    placeholders = ",".join("?" for _ in task_ids)
    return {
        row["task_id"]: dict(row)
        for row in db.execute(
            "SELECT t.id AS task_id,t.conversation_id,t.prompt,c.workspace,"
            "m.id AS message_id,m.content,m.created_at AS message_created_at "
            "FROM agent_tasks t JOIN conversations c ON c.id=t.conversation_id AND c.agent_id=t.agent_id "
            "JOIN messages m ON m.id=(SELECT first.id FROM messages first "
            "WHERE first.task_id=t.id AND first.conversation_id=t.conversation_id "
            "AND first.agent_id=t.agent_id AND first.role='user' ORDER BY first.id LIMIT 1) "
            f"WHERE t.agent_id=? AND t.id IN ({placeholders})",
            (agent_id, *sorted(task_ids)),
        )
    }


def _origin_in_scope(origin: dict[str, Any], scope: MemoryScope) -> bool:
    try:
        return bool(
            origin["workspace"] and str(workspace_root(origin["workspace"])) == scope.workspace
            and (scope.scope_type != "task" or origin["task_id"] == scope.scope_id)
            and (scope.scope_type != "conversation" or str(origin["conversation_id"]) == scope.scope_id)
        )
    except (ValueError, OSError, RuntimeError):
        return False


def _origin_revision(origin: dict[str, Any], scope: MemoryScope) -> str:
    return _digest([
        "siyi-memory-origin/v1", scope.agent_id, scope.workspace, scope.scope_type, scope.scope_id,
        origin["task_id"], origin["conversation_id"], origin["message_id"],
        origin["message_created_at"], origin["prompt"], origin["content"],
    ])


def bind_task_origin(db: sqlite3.Connection, scope: MemoryScope, task_id: str, content: str) -> dict[str, Any]:
    origin = read_task_origins(db, {task_id}, scope.agent_id).get(task_id)
    # Bind the original persisted user submission, not an arbitrary later
    # message (including queued steering) or IDs authored by a model/client.
    if origin is None or not _origin_in_scope(origin, scope) or origin["prompt"] != origin["content"]:
        raise MemorySourceError("工程记忆没有同工作区的有效任务来源，未保存")
    return {
        "version": 1,
        "task_id": task_id,
        "conversation_id": str(origin["conversation_id"]),
        "message_id": str(origin["message_id"]),
        "source_revision": _origin_revision(origin, scope),
        "content_revision": content_revision(content),
    }


def lineage_state(item: dict[str, Any], scope: MemoryScope, origins: dict[str, dict[str, Any]]) -> str:
    metadata = item["source_metadata"]
    if LINEAGE_KEY not in metadata:
        return "user_authored" if item["source"] == "user" else "legacy_unverified"
    lineage = metadata[LINEAGE_KEY]
    if (
        item["source"] not in GENERATED_SOURCES or not isinstance(lineage, dict)
        or type(lineage.get("version")) is not int or lineage["version"] != 1
        or any(not isinstance(lineage.get(field), str) or not lineage[field] for field in (
            "task_id", "conversation_id", "message_id", "source_revision", "content_revision",
        ))
        or any(lineage[field] != item.get(f"source_{field}") for field in ("task_id", "conversation_id", "message_id"))
    ):
        return "invalid"
    origin = origins.get(lineage["task_id"])
    if origin is None:
        return "source_missing"
    if not _origin_in_scope(origin, scope):
        return "source_outside_scope"
    if lineage["source_revision"] != _origin_revision(origin, scope):
        return "source_changed"
    if lineage["content_revision"] != content_revision(item["content"]):
        return "memory_changed"
    return "source_matched"


def revision_reference(item: dict[str, Any]) -> dict[str, Any]:
    """Metadata-only snapshot of what was selected, not a pointer to mutable text."""
    state = item["lineage_state"]
    lineage = item["source_metadata"].get(LINEAGE_KEY) if state == "source_matched" else None
    reference = {
        "memory_id": str(item["id"]),
        "memory_revision": _digest([
            "siyi-memory-revision/v1", item["record_id"], item["key"], item["category"],
            item["content"], item["tags"], item.get("applicable_version"), item["source"],
        ]),
        "lineage_state": state,
    }
    if lineage is not None:
        reference.update({key: lineage[key] for key in ("task_id", "conversation_id", "message_id", "source_revision")})
    return reference


def eligible_for_context(item: dict[str, Any]) -> bool:
    return not item.get("rejected") and item["lineage_state"] in {"source_matched", "user_authored", "legacy_unverified"}
