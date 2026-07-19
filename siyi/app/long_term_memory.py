from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .admin_action_grants import AdminActionAuthorization, require_admin_authorization
from .database import audit, connect, now_iso, rows
from .identity import ADMINISTRATOR_ID, AGENT_ID


MEMORY_TYPES = {"semantic", "episodic", "procedural", "relationship"}
MEMORY_STATUSES = {"candidate", "active", "superseded", "expired", "rejected", "deleted", "archived"}
SOURCE_TYPES = {"conversation", "user_confirmed", "system_observation", "imported_document", "manual_entry", "agent_inference"}
_SENSITIVE = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|api[_ -]?key|token\s*[:=]|密码|密钥|身份证|银行卡)", re.IGNORECASE)
_SOURCE_PRIORITY = {
    "agent_inference": 1,
    "system_observation": 2,
    "imported_document": 3,
    "manual_entry": 4,
    "conversation": 5,
    "user_confirmed": 6,
}


class MemoryConflictError(ValueError):
    def __init__(self, message: str, *, existing_memory_id: str) -> None:
        super().__init__(message)
        self.existing_memory_id = existing_memory_id


def normalize_content(content: str) -> str:
    return " ".join(content.strip().casefold().split())


def _public(item: dict[str, Any]) -> dict[str, Any]:
    item["user_confirmed"] = bool(item.get("user_confirmed"))
    item["is_locked"] = bool(item.get("is_locked"))
    item["is_sensitive"] = bool(item.get("is_sensitive"))
    try:
        item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
    except (TypeError, ValueError):
        item["metadata"] = {}
    return item


def create_memory(
    *,
    memory_type: str,
    content: str,
    title: str | None = None,
    source_type: str = "manual_entry",
    confidence: float = 0.8,
    importance: float = 0.5,
    emotional_weight: float = 0.0,
    occurred_at: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    status: str = "active",
    supersedes_memory_id: str | None = None,
    user_confirmed: bool = False,
    is_locked: bool = False,
    is_sensitive: bool | None = None,
    source_conversation_id: str | None = None,
    source_message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    allow_restore: bool = False,
) -> dict[str, Any]:
    if memory_type not in MEMORY_TYPES:
        raise ValueError("Unsupported memory type")
    if source_type not in SOURCE_TYPES:
        raise ValueError("Unsupported memory source")
    if status not in MEMORY_STATUSES:
        raise ValueError("Unsupported memory status")
    content = content.strip()
    if not content or len(content) > 8000:
        raise ValueError("Memory content must contain 1 to 8000 characters")
    normalized = normalize_content(content)
    deleted = rows(
        "SELECT id FROM memories WHERE agent_id=? AND user_id=? AND normalized_content=? AND status='deleted' LIMIT 1",
        (AGENT_ID, ADMINISTRATOR_ID, normalized),
    )
    if deleted and not allow_restore:
        raise MemoryConflictError("A deleted memory cannot be restored automatically", existing_memory_id=deleted[0]["id"])
    duplicate = rows(
        "SELECT * FROM memories WHERE agent_id=? AND user_id=? AND normalized_content=? AND status NOT IN ('deleted','rejected') LIMIT 1",
        (AGENT_ID, ADMINISTRATOR_ID, normalized),
    )
    if duplicate:
        return _public(duplicate[0])
    metadata = dict(metadata or {})
    subject = str(metadata.get("subject") or "").strip().casefold()
    predicate = str(metadata.get("predicate") or "").strip().casefold()
    if subject and predicate:
        metadata["subject"] = subject
        metadata["predicate"] = predicate
        conflicts = rows(
            "SELECT * FROM memories WHERE agent_id=? AND user_id=? AND status='active' "
            "AND json_extract(metadata_json, '$.subject')=? AND json_extract(metadata_json, '$.predicate')=? "
            "ORDER BY user_confirmed DESC, updated_at DESC LIMIT 1",
            (AGENT_ID, ADMINISTRATOR_ID, subject, predicate),
        )
        if conflicts and normalize_content(conflicts[0]["content"]) != normalized:
            previous = _public(conflicts[0])
            if previous["is_locked"]:
                raise MemoryConflictError("Locked memory conflict requires administrator resolution", existing_memory_id=previous["id"])
            previous_priority = _SOURCE_PRIORITY.get(str(previous["source_type"]), 0)
            current_priority = _SOURCE_PRIORITY.get(source_type, 0)
            if current_priority >= previous_priority and (user_confirmed or source_type == "user_confirmed"):
                supersedes_memory_id = previous["id"]
                with connect() as db:
                    db.execute(
                        "UPDATE memories SET status='superseded',valid_until=?,updated_at=? WHERE id=?",
                        (valid_from or now_iso(), now_iso(), previous["id"]),
                    )
            else:
                status = "candidate"
                metadata["conflicts_with"] = previous["id"]
    memory_id = f"mem_{uuid.uuid4().hex}"
    now = now_iso()
    sensitive = bool(_SENSITIVE.search(content)) if is_sensitive is None else bool(is_sensitive)
    values = (
        memory_id, AGENT_ID, ADMINISTRATOR_ID, memory_type, title, content, normalized,
        source_type, source_conversation_id, source_message_id, max(0.0, min(1.0, confidence)),
        max(0.0, min(1.0, importance)), max(-1.0, min(1.0, emotional_weight)), now, now,
        occurred_at, valid_from or now, valid_until, status, supersedes_memory_id,
        int(user_confirmed), int(is_locked), int(sensitive), json.dumps(metadata, ensure_ascii=False),
    )
    with connect() as db:
        db.execute(
            "INSERT INTO memories(id,agent_id,user_id,memory_type,title,content,normalized_content,source_type,"
            "source_conversation_id,source_message_id,confidence,importance,emotional_weight,created_at,updated_at,"
            "occurred_at,valid_from,valid_until,status,supersedes_memory_id,user_confirmed,is_locked,is_sensitive,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    audit(None, "long_term_memory_created", memory_id, "ok", {"type": memory_type, "source": source_type, "sensitive": sensitive})
    return get_memory(memory_id)


def get_memory(memory_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
    query = "SELECT * FROM memories WHERE id=? AND agent_id=?"
    params: tuple[Any, ...] = (memory_id, AGENT_ID)
    if not include_deleted:
        query += " AND status!='deleted'"
    items = rows(query, params)
    if not items:
        raise KeyError("Memory does not exist")
    return _public(items[0])


def list_memories(*, memory_type: str | None = None, status: str = "active", include_sensitive: bool = True) -> list[dict[str, Any]]:
    clauses = ["agent_id=?", "user_id=?"]
    params: list[Any] = [AGENT_ID, ADMINISTRATOR_ID]
    if memory_type:
        if memory_type not in MEMORY_TYPES:
            raise ValueError("Unsupported memory type")
        clauses.append("memory_type=?")
        params.append(memory_type)
    if status:
        clauses.append("status=?")
        params.append(status)
    if not include_sensitive:
        clauses.append("is_sensitive=0")
    items = rows(f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY is_locked DESC, importance DESC, updated_at DESC", tuple(params))
    return [_public(item) for item in items]


def update_memory(
    memory_id: str, changes: dict[str, Any], *, authorization: AdminActionAuthorization | None
) -> dict[str, Any]:
    require_admin_authorization(
        authorization, operation="memory.update", target_id=memory_id, payload=changes
    )
    current = get_memory(memory_id)
    allowed = {"title", "content", "memory_type", "confidence", "importance", "emotional_weight", "occurred_at", "valid_from", "valid_until", "status", "user_confirmed", "is_locked", "is_sensitive", "metadata"}
    updates = {key: value for key, value in changes.items() if key in allowed and value is not None}
    if not updates:
        return current
    if "memory_type" in updates and updates["memory_type"] not in MEMORY_TYPES:
        raise ValueError("Unsupported memory type")
    if "status" in updates and updates["status"] not in MEMORY_STATUSES:
        raise ValueError("Unsupported memory status")
    if "content" in updates:
        updates["content"] = str(updates["content"]).strip()
        updates["normalized_content"] = normalize_content(updates["content"])
    if "metadata" in updates:
        updates["metadata_json"] = json.dumps(updates.pop("metadata"), ensure_ascii=False)
    for key in ("user_confirmed", "is_locked", "is_sensitive"):
        if key in updates:
            updates[key] = int(bool(updates[key]))
    for key in ("confidence", "importance"):
        if key in updates:
            updates[key] = max(0.0, min(1.0, float(updates[key])))
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in updates)
    with connect() as db:
        db.execute(f"UPDATE memories SET {assignments} WHERE id=? AND agent_id=?", (*updates.values(), memory_id, AGENT_ID))
    audit(None, "long_term_memory_updated", memory_id, "ok", {"fields": sorted(updates)})
    return get_memory(memory_id)


def delete_memory(memory_id: str, *, authorization: AdminActionAuthorization | None) -> dict[str, Any]:
    require_admin_authorization(
        authorization, operation="memory.delete", target_id=memory_id, payload={}
    )
    current = get_memory(memory_id)
    deleted_at = now_iso()
    metadata = dict(current.get("metadata") or {})
    metadata["deleted_at"] = deleted_at
    metadata["deletion_fingerprint"] = hashlib.sha256(current["normalized_content"].encode()).hexdigest()
    with connect() as db:
        db.execute(
            "UPDATE memories SET status='deleted', metadata_json=?, updated_at=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), deleted_at, memory_id),
        )
    audit(None, "long_term_memory_deleted", memory_id, "ok", {"locked": current["is_locked"]})
    return {"id": memory_id, "deleted": True}


def retrieve_memories(query: str, *, limit: int = 8, include_sensitive: bool = False, memory_type: str | None = None) -> list[dict[str, Any]]:
    normalized_query = normalize_content(query)
    terms = {term for term in re.findall(r"[a-z0-9_.:-]{2,}", normalized_query) if len(term) > 1}
    for sequence in re.findall(r"[\u4e00-\u9fff]+", normalized_query):
        for size in (2, 3, 4):
            terms.update(sequence[index:index + size] for index in range(max(0, len(sequence) - size + 1)))
    candidates = list_memories(memory_type=memory_type, status="active", include_sensitive=include_sensitive)
    now = datetime.now(timezone.utc)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for item in candidates:
        haystack = normalize_content(f"{item.get('title') or ''} {item['content']}")
        overlap = sum(1 for term in terms if term in haystack)
        if terms and not overlap and not item["is_locked"]:
            continue
        try:
            age_days = max(0.0, (now - datetime.fromisoformat(item["updated_at"])).total_seconds() / 86400)
        except ValueError:
            age_days = 365.0
        recency = 1 / (1 + age_days / 90)
        score = (overlap / max(len(terms), 1)) * 0.45 + float(item["importance"]) * 0.25 + float(item["confidence"]) * 0.2 + recency * 0.1
        if item["user_confirmed"]:
            score += 0.12
        if item["is_locked"]:
            score += 0.08
        item["retrieval_score"] = round(score, 4)
        ranked.append((score, item))
    selected = [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[: max(1, min(limit, 20))]]
    if selected:
        with connect() as db:
            for item in selected:
                db.execute("UPDATE memories SET access_count=access_count+1,last_accessed_at=? WHERE id=?", (now_iso(), item["id"]))
    return selected


def memory_history(memory_id: str) -> list[dict[str, Any]]:
    all_items = [_public(item) for item in rows("SELECT * FROM memories WHERE agent_id=?", (AGENT_ID,))]
    by_id = {item["id"]: item for item in all_items}
    current = by_id.get(memory_id)
    if current is None:
        raise KeyError("Memory does not exist")
    chain = [current]
    seen = {memory_id}
    while current.get("supersedes_memory_id") and current["supersedes_memory_id"] not in seen:
        parent = by_id.get(str(current["supersedes_memory_id"]))
        if parent is None:
            break
        chain.append(parent)
        seen.add(parent["id"])
        current = parent
    children = [item for item in all_items if item.get("supersedes_memory_id") == memory_id and item["id"] not in seen]
    return [*children, *chain]


def submit_candidate(
    *, memory_type: str, content: str, reason: str, confidence: float, importance: float,
    source_conversation_id: str | None = None, is_sensitive: bool | None = None,
) -> dict[str, Any]:
    if memory_type not in MEMORY_TYPES:
        raise ValueError("Unsupported memory type")
    normalized = normalize_content(content)
    if not normalized:
        raise ValueError("Candidate content is required")
    duplicate = rows(
        "SELECT * FROM memory_candidates WHERE agent_id=? AND lower(trim(content))=? AND status='pending' LIMIT 1",
        (AGENT_ID, normalized),
    )
    if duplicate:
        return duplicate[0]
    candidate_id = f"mc_{uuid.uuid4().hex}"
    sensitive = bool(_SENSITIVE.search(content)) if is_sensitive is None else bool(is_sensitive)
    with connect() as db:
        db.execute(
            "INSERT INTO memory_candidates(id,agent_id,user_id,memory_type,content,reason,confidence,importance,"
            "is_sensitive,source_conversation_id,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (candidate_id, AGENT_ID, ADMINISTRATOR_ID, memory_type, content[:8000], reason[:1000], max(0.0, min(1.0, confidence)), max(0.0, min(1.0, importance)), int(sensitive), source_conversation_id, "pending", now_iso()),
        )
    return rows("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,))[0]


def extract_explicit_candidates(content: str, *, conversation_id: int | None = None) -> list[dict[str, Any]]:
    text = content.strip()
    if not text or not re.search(r"(?:请|帮我)?记住|以后(?:都|请)|我的(?:偏好|习惯|原则)|当前主要", text):
        return []
    fragments = [part.strip(" ，。！？\n") for part in re.split(r"[。！？\n]+", text) if part.strip()]
    results: list[dict[str, Any]] = []
    for fragment in fragments[:3]:
        if not re.search(r"(?:记住|以后|我的|当前主要)", fragment):
            continue
        memory_type = "procedural" if re.search(r"(?:以后|习惯|原则|不要|优先)", fragment) else "semantic"
        results.append(
            submit_candidate(
                memory_type=memory_type,
                content=fragment[:8000],
                reason="explicit user statement selected by deterministic candidate extractor",
                confidence=0.9,
                importance=0.75,
                source_conversation_id=str(conversation_id) if conversation_id is not None else None,
            )
        )
    return results


def decide_candidate(
    candidate_id: str,
    *,
    accept: bool,
    authorization: AdminActionAuthorization | None = None,
) -> dict[str, Any]:
    items = rows("SELECT * FROM memory_candidates WHERE id=? AND status='pending'", (candidate_id,))
    if not items:
        raise KeyError("Memory candidate does not exist or was already decided")
    item = items[0]
    if accept:
        require_admin_authorization(
            authorization,
            operation="memory_candidate.accept",
            target_id=candidate_id,
            payload={"accept": True},
        )
    accepted = accept
    status = "accepted" if accepted else "rejected"
    result: dict[str, Any] = {"candidate_id": candidate_id, "status": status}
    if accepted:
        result["memory"] = create_memory(
            memory_type=item["memory_type"], content=item["content"], source_type="user_confirmed",
            confidence=item["confidence"], importance=item["importance"], user_confirmed=True,
            is_sensitive=bool(item["is_sensitive"]), source_conversation_id=item["source_conversation_id"],
        )
    with connect() as db:
        db.execute(
            "UPDATE memory_candidates SET status=?,decision_reason=?,decided_at=? WHERE id=?",
            (status, "administrator decision", now_iso(), candidate_id),
        )
    audit(None, "memory_candidate_decided", candidate_id, "ok", {"status": status})
    return result
