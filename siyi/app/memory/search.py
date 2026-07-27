from __future__ import annotations

import json
import math
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from app.database import connect
from app.memory.long_term import MEMORY_STATUSES, MEMORY_TYPES, SOURCE_TYPES, normalize_content
from app.personality.identity_service import ADMINISTRATOR_ID, AGENT_ID
from app.security.trust import REDACTED, redact_payload


SensitiveMode = Literal["exclude", "redacted", "full"]


@dataclass(frozen=True)
class MemorySearchQuery:
    query: str
    memory_types: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ("active",)
    source_types: tuple[str, ...] = ()
    user_confirmed: bool | None = None
    is_locked: bool | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    min_importance: float | None = None
    min_confidence: float | None = None
    sensitive_mode: SensitiveMode = "exclude"
    sort: Literal["relevance", "updated", "importance"] = "relevance"
    offset: int = 0
    limit: int = 20
    cursor: str | None = None


def _cursor_offset(cursor: str | None, fallback: int) -> int:
    if not cursor:
        return fallback
    try:
        decoded = urlsafe_b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4)).decode("ascii")
        prefix, value = decoded.split(":", 1)
        if prefix != "memory-search":
            raise ValueError
        return int(value)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid search cursor") from exc


def _encode_cursor(offset: int) -> str:
    return urlsafe_b64encode(f"memory-search:{offset}".encode("ascii")).decode("ascii").rstrip("=")


def _query_terms(query: str) -> list[str]:
    normalized = normalize_content(query)
    terms = re.findall(r"[a-z0-9_.:-]{2,}|[\u4e00-\u9fff]+", normalized)
    return list(dict.fromkeys(term[:100] for term in terms if term.strip()))[:20]


def _fts_expression(terms: list[str]) -> str:
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _parse_metadata(metadata_json: str) -> dict[str, Any]:
    try:
        value = json.loads(metadata_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_tags(metadata_json: str) -> list[str]:
    tags = _parse_metadata(metadata_json).get("tags")
    return [str(item) for item in tags] if isinstance(tags, list) else []


def _recency(updated_at: str) -> float:
    try:
        stamp = datetime.fromisoformat(updated_at)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400)
    except (TypeError, ValueError):
        age_days = 365.0
    return 1 / (1 + age_days / 90)


class MemorySearchService:
    def search(self, request: MemorySearchQuery) -> dict[str, Any]:
        query = request.query.strip()
        if not query or len(query) > 500:
            raise ValueError("Search query must contain 1 to 500 characters")
        if request.offset < 0 or request.limit < 1 or request.limit > 100:
            raise ValueError("Invalid pagination")
        invalid_types = set(request.memory_types) - MEMORY_TYPES
        invalid_statuses = set(request.statuses) - MEMORY_STATUSES
        invalid_sources = set(request.source_types) - SOURCE_TYPES
        if invalid_types:
            raise ValueError(f"Unsupported memory type: {sorted(invalid_types)[0]}")
        if invalid_statuses:
            raise ValueError(f"Unsupported memory status: {sorted(invalid_statuses)[0]}")
        if invalid_sources:
            raise ValueError(f"Unsupported memory source: {sorted(invalid_sources)[0]}")
        for value in (request.min_importance, request.min_confidence):
            if value is not None and not 0 <= value <= 1:
                raise ValueError("Search score filters must be between 0 and 1")
        page_offset = _cursor_offset(request.cursor, request.offset)
        if page_offset < 0:
            raise ValueError("Invalid search cursor")

        terms = _query_terms(query)
        if not terms:
            terms = [normalize_content(query)[:100]]
        fts_ranks: dict[str, float] = {}
        with connect() as db:
            try:
                for row in db.execute(
                    "SELECT memory_id,bm25(memories_fts,0.0,3.0,1.5,1.0) AS rank "
                    "FROM memories_fts WHERE memories_fts MATCH ? LIMIT 1000",
                    (_fts_expression(terms),),
                ):
                    fts_ranks[str(row["memory_id"])] = float(row["rank"])
            except Exception as exc:
                raise RuntimeError("Long-term memory search index is unavailable") from exc

            clauses = ["agent_id=?", "user_id=?"]
            params: list[Any] = [AGENT_ID, ADMINISTRATOR_ID]
            if request.memory_types:
                clauses.append(f"memory_type IN ({','.join('?' for _ in request.memory_types)})")
                params.extend(request.memory_types)
            if request.statuses:
                clauses.append(f"status IN ({','.join('?' for _ in request.statuses)})")
                params.extend(request.statuses)
            if request.source_types:
                clauses.append(f"source_type IN ({','.join('?' for _ in request.source_types)})")
                params.extend(request.source_types)
            if request.user_confirmed is not None:
                clauses.append("user_confirmed=?")
                params.append(int(request.user_confirmed))
            if request.is_locked is not None:
                clauses.append("is_locked=?")
                params.append(int(request.is_locked))
            if request.valid_from:
                clauses.append("(valid_until IS NULL OR valid_until>=?)")
                params.append(request.valid_from)
            if request.valid_to:
                clauses.append("(valid_from IS NULL OR valid_from<=?)")
                params.append(request.valid_to)
            if request.min_importance is not None:
                clauses.append("importance>=?")
                params.append(request.min_importance)
            if request.min_confidence is not None:
                clauses.append("confidence>=?")
                params.append(request.min_confidence)
            if request.sensitive_mode == "exclude":
                clauses.append("is_sensitive=0")
            records = [dict(row) for row in db.execute(
                f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT 5000",
                tuple(params),
            )]

        ranked: list[dict[str, Any]] = []
        for record in records:
            tags = _parse_tags(str(record.get("metadata_json") or "{}"))
            fields = {
                "title": normalize_content(str(record.get("title") or "")),
                "content": normalize_content(str(record.get("content") or "")),
                "tags": normalize_content(" ".join(tags)),
            }
            matched_fields = [name for name, value in fields.items() if any(term in value for term in terms)]
            fts_rank = fts_ranks.get(str(record["id"]))
            if fts_rank is None and not matched_fields:
                continue
            matched_terms = [term for term in terms if any(term in value for value in fields.values())]
            lexical = len(matched_terms) / max(len(terms), 1)
            title_bonus = 0.15 if "title" in matched_fields else 0.0
            tag_bonus = 0.1 if "tags" in matched_fields else 0.0
            fts_quality = 1 / (1 + math.exp(min(20.0, max(-20.0, float(fts_rank or 0.0)))))
            recency = _recency(str(record.get("updated_at") or ""))
            retrieval = (
                lexical * 0.45
                + float(record.get("importance") or 0) * 0.25
                + float(record.get("confidence") or 0) * 0.2
                + recency * 0.1
                + (0.12 if record.get("user_confirmed") else 0.0)
                + (0.08 if record.get("is_locked") else 0.0)
            )
            score = (
                lexical * 0.3
                + fts_quality * 0.15
                + min(retrieval, 1.0) * 0.25
                + title_bonus
                + tag_bonus
            )
            content = str(record.get("content") or "")
            title = str(record.get("title") or "") or None
            if record.get("is_sensitive") and request.sensitive_mode == "redacted":
                redacted, _ = redact_payload({"title": title, "content": content})
                title = "敏感记忆"
                content = str(redacted.get("content") or REDACTED)
                if content == str(record.get("content") or ""):
                    content = REDACTED
                matched_terms = []
                matched_fields = []
                tags = []
            metadata = _parse_metadata(str(record.get("metadata_json") or "{}"))
            ranked.append(
                {
                    "id": record["id"],
                    "namespace": "personal_long_term",
                    "memory_type": record["memory_type"],
                    "status": record["status"],
                    "title": title,
                    "content": content,
                    "snippet": content[:320],
                    "tags": tags,
                    "source_type": record.get("source_type"),
                    "is_sensitive": bool(record.get("is_sensitive")),
                    "is_locked": bool(record.get("is_locked")),
                    "user_confirmed": bool(record.get("user_confirmed")),
                    "importance": float(record.get("importance") or 0),
                    "confidence": float(record.get("confidence") or 0),
                    "emotional_weight": float(record.get("emotional_weight") or 0),
                    "occurred_at": record.get("occurred_at"),
                    "valid_from": record.get("valid_from"),
                    "valid_until": record.get("valid_until"),
                    "created_at": record.get("created_at"),
                    "updated_at": record.get("updated_at"),
                    "metadata": metadata if request.sensitive_mode == "full" or not record.get("is_sensitive") else {},
                    "matched_fields": matched_fields,
                    "matched_terms": matched_terms,
                    "score": round(score, 6),
                    "ranking": {
                        "lexical": round(lexical, 6),
                        "fts_rank": round(float(fts_rank), 6) if fts_rank is not None else None,
                        "importance": float(record.get("importance") or 0),
                        "confidence": float(record.get("confidence") or 0),
                        "recency": round(recency, 6),
                        "retrieval": round(retrieval, 6),
                    },
                }
            )

        if request.sort == "updated":
            ranked.sort(key=lambda item: (str(item["updated_at"]), item["score"]), reverse=True)
        elif request.sort == "importance":
            ranked.sort(key=lambda item: (item["importance"], item["score"]), reverse=True)
        else:
            ranked.sort(key=lambda item: (item["score"], str(item["updated_at"])), reverse=True)
        total = len(ranked)
        items = ranked[page_offset : page_offset + request.limit]
        next_offset = page_offset + len(items)
        has_more = next_offset < total
        return {
            "query": query,
            "items": items,
            "page": {
                "offset": page_offset,
                "limit": request.limit,
                "total": total,
                "has_more": has_more,
                "next_cursor": _encode_cursor(next_offset) if has_more else None,
            },
            "filters": {
                "memory_types": list(request.memory_types),
                "statuses": list(request.statuses),
                "source_types": list(request.source_types),
                "user_confirmed": request.user_confirmed,
                "is_locked": request.is_locked,
                "valid_from": request.valid_from,
                "valid_to": request.valid_to,
                "min_importance": request.min_importance,
                "min_confidence": request.min_confidence,
                "sensitive_mode": request.sensitive_mode,
                "sort": request.sort,
            },
        }


memory_search_service = MemorySearchService()
