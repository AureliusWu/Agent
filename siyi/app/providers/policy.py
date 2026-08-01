from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Literal

from app.database import connect, now_iso, rows


CapabilityLevel = Literal["FULL", "PARTIAL", "UNSUPPORTED"]


@dataclass(frozen=True)
class ProviderPolicy:
    id: str
    preferred_provider: str
    preferred_model: str
    allow_paid_fallback: bool = False
    fallback_order: tuple[str, ...] = ()
    authorization_source: str | None = None
    task_id: str | None = None


def create_provider_policy(
    preferred_provider: str,
    preferred_model: str,
    *,
    task_id: str | None = None,
    allow_paid_fallback: bool = False,
    fallback_order: tuple[str, ...] = (),
    authorization_source: str | None = None,
) -> ProviderPolicy:
    if allow_paid_fallback and not authorization_source:
        raise ValueError("paid fallback requires explicit authorization")
    if preferred_provider == "ollama" and any(item != "ollama" for item in fallback_order) and not authorization_source:
        raise ValueError("local provider cannot silently fall back to a remote provider")
    policy = ProviderPolicy(uuid.uuid4().hex, preferred_provider, preferred_model, allow_paid_fallback, fallback_order, authorization_source, task_id)
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO provider_policies(id,task_id,preferred_provider,preferred_model,allow_paid_fallback,fallback_order,authorization_source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (policy.id, task_id, preferred_provider, preferred_model, int(allow_paid_fallback), json.dumps(fallback_order), authorization_source, stamp, stamp),
        )
    return policy


def authorize_provider_switch(policy_id: str, authorization_source: str) -> ProviderPolicy:
    if not authorization_source.strip():
        raise ValueError("authorization source is required")
    with connect() as db:
        db.execute("UPDATE provider_policies SET allow_paid_fallback=1,authorization_source=?,updated_at=? WHERE id=?", (authorization_source, now_iso(), policy_id))
    records = rows("SELECT * FROM provider_policies WHERE id=?", (policy_id,))
    if not records:
        raise ValueError("provider policy does not exist")
    item = records[0]
    return ProviderPolicy(item["id"], item["preferred_provider"], item["preferred_model"], bool(item["allow_paid_fallback"]), tuple(json.loads(item["fallback_order"])), item["authorization_source"], item["task_id"])


def classify_local_capability(capability: str) -> CapabilityLevel:
    if capability in {"chat", "summarization", "classification", "planning", "code_explanation"}:
        return "FULL"
    if capability in {"tool_calling", "structured_output", "long_context", "code_generation"}:
        return "PARTIAL"
    return "UNSUPPORTED"
