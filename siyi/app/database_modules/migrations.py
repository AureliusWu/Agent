from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .schema import LONG_TERM_MEMORY_COMPATIBILITY_TRIGGERS, SCOPED_MEMORY_SCHEMA


def normalize_memory_content(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def memory_fingerprint(value: Any) -> str:
    return hashlib.sha256(normalize_memory_content(value).encode("utf-8")).hexdigest()


def migration_v45(db: sqlite3.Connection) -> None:
    """Introduce the scoped Memory v2 store without deleting legacy records.

    ``workspace_memories`` and ``memories`` remain intact as rollback and
    compatibility sources. Workspace integer ids and long-term string ids are
    retained through explicit legacy mappings so existing API bookmarks do not
    change when an installation upgrades.
    """

    db.create_function("memory_normalize", 1, normalize_memory_content, deterministic=True)
    db.create_function("memory_fingerprint", 1, memory_fingerprint, deterministic=True)
    db.executescript(SCOPED_MEMORY_SCHEMA)
    legacy_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_memories'"
    ).fetchone()
    if legacy_exists:
        for row in db.execute("SELECT * FROM workspace_memories ORDER BY id").fetchall():
            item = dict(row)
            namespace = str(item.get("namespace") or "project")
            workspace = str(item.get("workspace") or "")
            scope_type = "user" if namespace == "personal" else "workspace"
            scope_id = "administrator-001" if scope_type == "user" else workspace
            content = str(item.get("content") or "")
            metadata = {
                "migrated_from": "workspace_memories",
                "legacy_namespace": namespace,
                "legacy_source": str(item.get("source") or "legacy"),
            }
            db.execute(
                "INSERT OR IGNORE INTO memory_records("
                "id,record_id,agent_id,scope_type,scope_id,workspace,key,content,normalized_content,content_fingerprint,"
                "kind,category,source_type,source_task_id,source_metadata_json,tags,applicable_version,"
                "project_signature,confidence,last_verified_at,last_used_at,use_count,success_count,failure_count,"
                "rejected,invalidated_reason,legacy_table,legacy_id,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    int(item["id"]),
                    f"workspace-memory:{item['id']}",
                    str(item.get("agent_id") or "natsume-kokoro-001"),
                    scope_type,
                    scope_id,
                    "" if scope_type == "user" else workspace,
                    str(item.get("key") or ""),
                    content,
                    normalize_memory_content(content),
                    memory_fingerprint(content),
                    str(item.get("kind") or "project"),
                    str(item.get("category") or "decision"),
                    str(item.get("source") or "legacy"),
                    item.get("source_task_id"),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    str(item.get("tags") or "[]"),
                    item.get("applicable_version"),
                    str(item.get("project_signature") or "{}"),
                    float(item.get("confidence") or 0.7),
                    item.get("last_verified_at"),
                    item.get("last_used_at"),
                    int(item.get("use_count") or 0),
                    int(item.get("success_count") or 0),
                    int(item.get("failure_count") or 0),
                    int(item.get("rejected") or 0),
                    item.get("invalidated_reason"),
                    "workspace_memories",
                    str(item["id"]),
                    str(item.get("created_at") or ""),
                    str(item.get("updated_at") or ""),
                ),
            )
    long_term_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
    ).fetchone()
    if long_term_exists:
        for row in db.execute("SELECT * FROM memories ORDER BY created_at,id").fetchall():
            item = dict(row)
            content = str(item.get("content") or "")
            status = str(item.get("status") or "active")
            db.execute(
                "INSERT OR IGNORE INTO memory_records("
                "record_id,agent_id,scope_type,scope_id,workspace,key,content,normalized_content,content_fingerprint,"
                "kind,category,memory_type,title,source_type,source_conversation_id,source_message_id,"
                "source_metadata_json,tags,project_signature,confidence,importance,emotional_weight,access_count,"
                "last_accessed_at,occurred_at,valid_from,valid_until,status,supersedes_record_id,user_confirmed,"
                "is_locked,is_sensitive,metadata_json,rejected,legacy_table,legacy_id,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(item["id"]),
                    str(item.get("agent_id") or "natsume-kokoro-001"),
                    "user",
                    str(item.get("user_id") or "administrator-001"),
                    "",
                    f"long-term:{item['id']}",
                    content,
                    normalize_memory_content(content),
                    memory_fingerprint(content),
                    "personal",
                    "long_term",
                    str(item.get("memory_type") or "semantic"),
                    item.get("title"),
                    str(item.get("source_type") or "manual_entry"),
                    item.get("source_conversation_id"),
                    item.get("source_message_id"),
                    json.dumps({"compatibility_source": "memories"}, ensure_ascii=False, sort_keys=True),
                    "[]",
                    "{}",
                    float(item.get("confidence") or 0.5),
                    float(item.get("importance") or 0.5),
                    float(item.get("emotional_weight") or 0),
                    int(item.get("access_count") or 0),
                    item.get("last_accessed_at"),
                    item.get("occurred_at"),
                    item.get("valid_from"),
                    item.get("valid_until"),
                    status,
                    item.get("supersedes_memory_id"),
                    int(item.get("user_confirmed") or 0),
                    int(item.get("is_locked") or 0),
                    int(item.get("is_sensitive") or 0),
                    str(item.get("metadata_json") or "{}"),
                    int(status in {"deleted", "rejected"}),
                    "memories",
                    str(item["id"]),
                    str(item.get("created_at") or ""),
                    str(item.get("updated_at") or ""),
                ),
            )
    db.executescript(LONG_TERM_MEMORY_COMPATIBILITY_TRIGGERS)
