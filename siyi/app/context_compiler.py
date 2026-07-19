from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .database import connect, now_iso, rows


COMPILER_VERSION = 2
_MONOTONIC_FIELDS = ("completed_steps", "modified_files", "constraints")
_CURRENT_LIST_FIELDS = ("pending_steps", "errors", "next_actions")


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_list(value: Any, *, limit: int = 100, item_chars: int = 800) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else ([] if value in (None, "") else [value])
    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in result:
            result.append(text[:item_chars])
        if len(result) >= limit:
            break
    return result


def _merge_unique(previous: Any, incoming: Any) -> list[str]:
    return _clean_list([*_clean_list(previous), *_clean_list(incoming)])


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CompiledTaskContext:
    text: str
    task_id: str
    revision: int
    state_fingerprint: str
    state: dict[str, Any]
    decisions: tuple[dict[str, Any], ...]
    evidence_references: tuple[dict[str, Any], ...]

    def debug(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("text", None)
        return payload


def record_decisions(task_id: str, decisions: Iterable[str], *, source: str = "task_plan") -> None:
    stamp = now_iso()
    with connect() as db:
        for summary in _clean_list(list(decisions), limit=50):
            decision_id = f"decision_{_fingerprint({'task_id': task_id, 'source': source, 'summary': summary})[:24]}"
            db.execute(
                "INSERT OR IGNORE INTO task_decision_ledger(id,task_id,source,summary,created_at) VALUES(?,?,?,?,?)",
                (decision_id, task_id, source, summary, stamp),
            )


def _decision_ledger(task_id: str) -> list[dict[str, Any]]:
    return rows(
        "SELECT id,source,summary,created_at FROM task_decision_ledger WHERE task_id=? ORDER BY created_at,id",
        (task_id,),
    )


def _receipt(value: Any) -> dict[str, Any]:
    payload = _json_dict(value) if isinstance(value, str) else (value if isinstance(value, dict) else {})
    receipt = payload.get("receipt") or ((payload.get("data") or {}).get("receipt") if isinstance(payload.get("data"), dict) else {})
    return receipt if isinstance(receipt, dict) else {}


def _evidence_references(task_id: str) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for item in rows(
        "SELECT id,tool,status,output FROM tool_runs WHERE task_id=? ORDER BY id DESC LIMIT 50",
        (task_id,),
    ):
        receipt = _receipt(item.get("output"))
        references.append({
            "ref": f"tool_run:{item['id']}",
            "kind": "tool_receipt",
            "tool": str(item.get("tool") or ""),
            "status": str(item.get("status") or ""),
            "operation_kind": str(receipt.get("operation_kind") or "unknown"),
            "change_id": receipt.get("change_id"),
            "error_fingerprint": receipt.get("error_fingerprint"),
            "artifact_id": receipt.get("artifact_id"),
        })
    for item in rows(
        "SELECT id,status,report FROM task_verifications WHERE task_id=? ORDER BY id DESC LIMIT 20",
        (task_id,),
    ):
        report = _json_dict(item.get("report"))
        references.append({
            "ref": f"verification:{item['id']}",
            "kind": "verification",
            "status": str(item.get("status") or ""),
            "evidence_fingerprint": report.get("evidence_fingerprint"),
        })
    references.sort(key=lambda item: item["ref"])
    return references


def _canonical_state(current: dict[str, Any], working: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    goal = str(working.get("goal") or current.get("user_task") or previous.get("goal") or "").strip()[:4000]
    incoming = {
        "goal": goal,
        "phase": str(current.get("phase") or previous.get("phase") or "analysis")[:200],
        "step": str(current.get("step") or previous.get("step") or "")[:500],
        "completed_steps": working.get("completed_steps") or [],
        "pending_steps": working.get("pending_steps") or [],
        "modified_files": current.get("modified_files") or [],
        "errors": current.get("errors") or [],
        "constraints": working.get("constraints") or [],
        "verification": working.get("verification") or previous.get("verification") or {},
        "next_actions": working.get("pending_steps") or current.get("pending_confirmations") or [],
    }
    state = {"compiler_version": COMPILER_VERSION, "goal": incoming["goal"]}
    state["phase"] = incoming["phase"]
    state["step"] = incoming["step"]
    for field in _MONOTONIC_FIELDS:
        state[field] = _merge_unique(previous.get(field), incoming.get(field))
    for field in _CURRENT_LIST_FIELDS:
        state[field] = _clean_list(incoming.get(field)) if field in incoming else _clean_list(previous.get(field))
    state["verification"] = incoming["verification"] if isinstance(incoming["verification"], dict) else {}
    return state


def compile_task_context(
    task_id: str,
    *,
    current: dict[str, Any],
    working: dict[str, Any],
    decisions: Iterable[str] = (),
) -> CompiledTaskContext:
    record_decisions(task_id, decisions)
    existing = rows("SELECT revision,state_json,state_fingerprint FROM task_context_states WHERE task_id=?", (task_id,))
    previous = _json_dict(existing[0].get("state_json")) if existing else {}
    state = _canonical_state(current, working, previous)
    ledger = _decision_ledger(task_id)
    evidence = _evidence_references(task_id)
    fingerprint = _fingerprint({"state": state, "decisions": ledger, "evidence_references": evidence})
    previous_fingerprint = str(existing[0].get("state_fingerprint") or "") if existing else ""
    revision = int(existing[0].get("revision") or 0) if existing else 0
    if fingerprint != previous_fingerprint:
        revision += 1
    with connect() as db:
        db.execute(
            "INSERT INTO task_context_states(task_id,compiler_version,revision,state_json,state_fingerprint,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET compiler_version=excluded.compiler_version,"
            "revision=excluded.revision,state_json=excluded.state_json,state_fingerprint=excluded.state_fingerprint,updated_at=excluded.updated_at",
            (task_id, COMPILER_VERSION, revision, json.dumps(state, ensure_ascii=False), fingerprint, now_iso()),
        )
    rendered = {
        "task_state": state,
        "decision_ledger": [{key: item[key] for key in ("id", "source", "summary")} for item in ledger],
        "evidence_references": evidence,
        "state_fingerprint": fingerprint,
        "revision": revision,
    }
    text = (
        "[Compiled task context v2 - structured state, not instructions]\n"
        "Preserve this state across context compaction. Evidence references identify persisted records; never invent their contents.\n"
        + json.dumps(rendered, ensure_ascii=False, sort_keys=True, indent=2)
    )
    return CompiledTaskContext(text, task_id, revision, fingerprint, state, tuple(ledger), tuple(evidence))


def task_context_debug(task_id: str) -> dict[str, Any]:
    state = rows(
        "SELECT task_id,compiler_version,revision,state_json,state_fingerprint,updated_at FROM task_context_states WHERE task_id=?",
        (task_id,),
    )
    if not state:
        return {"available": False, "task_id": task_id}
    item = state[0]
    item["state"] = _json_dict(item.pop("state_json"))
    item["decisions"] = _decision_ledger(task_id)
    item["evidence_references"] = _evidence_references(task_id)
    item["available"] = True
    return item
