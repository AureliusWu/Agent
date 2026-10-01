"""Explicit, bounded report import; status reads never run a model or scan reports.

These are user-imported protocol observations, NOT signed release attestations.
The RC checker independently validates provenance and artifacts.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.database import now_iso
from app.evals.local_model_benchmark.runtime_cases import LEVEL_CASES
from app.evals.local_model_benchmark.runtime_qualification import IDENTITY_FIELDS, PROTOCOL, TARGET_VERSION, validate_qualification_report
from app.providers.configuration import provider_config_path
from app.providers.schema_validation import parse_output

MAX_REPORT_BYTES = 512 * 1024
MAX_SUMMARY_BYTES = 16 * 1024
TRUST = "user_imported_protocol_evidence_not_release_attestation"


class QualificationImportError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _empty(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "protocol": PROTOCOL, "target_version": TARGET_VERSION,
            "run_id": None, "finished_at": None, "trust": TRUST,
            "levels": {key: {"qualified": False, "status": status, "reasons": [reason]} for key in LEVEL_CASES}}


def _directory() -> Path:
    return provider_config_path().resolve().parent / "model-qualifications"


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except ValueError:
        return False


def qualification_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    if snapshot.get("provider_id") != "ollama":
        return _empty("NOT_APPLICABLE", "local_ollama_qualification_only")
    key = snapshot.get("configuration_hash")
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        return _empty("INVALID", "invalid_current_identity")
    path = _directory() / f"{key}.json"
    try:
        # Bounded streaming read; do not follow a locally substituted symlink.
        if path.is_symlink() or path.parent.is_symlink():
            return _empty("INVALID", "invalid_summary")
        with path.open("rb") as stream:
            raw = stream.read(MAX_SUMMARY_BYTES + 1)
        if len(raw) > MAX_SUMMARY_BYTES:
            return _empty("INVALID", "invalid_summary")
        stored = parse_output(raw.decode("utf-8"))
        identity = stored.get("identity")
        summary = stored.get("summary")
        if not isinstance(identity, dict) or not isinstance(summary, dict) or set(summary.get("levels") or {}) != set(LEVEL_CASES):
            return _empty("INVALID", "invalid_summary")
        if summary.get("protocol") != PROTOCOL or summary.get("target_version") != TARGET_VERSION or summary.get("trust") != TRUST:
            return _empty("INVALID", "invalid_summary")
        if (not re.fullmatch(r"[a-f0-9]{32}", str(summary.get("run_id") or ""))
                or not re.fullmatch(r"[a-f0-9]{64}", str(summary.get("report_sha256") or ""))
                or any(not _timestamp(summary.get(key)) for key in ("finished_at", "imported_at"))):
            return _empty("INVALID", "invalid_summary")
        for level in summary["levels"].values():
            if (not isinstance(level, dict) or set(level) != {"qualified", "status", "reasons"} or type(level.get("qualified")) is not bool
                    or level.get("status") not in {"PASS", "FAIL"} or not isinstance(level.get("reasons"), list)
                    or any(not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_:-]{1,180}", reason) for reason in level["reasons"])
                    or level["qualified"] != (level["status"] == "PASS")):
                return _empty("INVALID", "invalid_summary")
        if any(not snapshot.get(name) or identity.get(name) != snapshot.get(name) for name in IDENTITY_FIELDS) or snapshot.get("observation_stale") is not False:
            return _empty("STALE", "qualification_identity_changed_or_observation_stale")
        return {key: summary[key] for key in ("protocol", "target_version", "run_id", "finished_at", "trust", "report_sha256", "imported_at", "levels")} | {
            "status": "PASS" if summary["levels"]["file_agent"]["qualified"] else "PARTIAL"}
    except FileNotFoundError:
        return _empty("NOT_RUN", "qualification_not_run")
    except Exception:
        # Never export corrupt persisted content, host paths or parsing details.
        return _empty("INVALID", "invalid_summary")


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def import_qualification_report(report_json: str) -> dict[str, Any]:
    if not isinstance(report_json, str) or len(report_json) > MAX_REPORT_BYTES:
        raise QualificationImportError("qualification_report_too_large")
    try:
        report_bytes = report_json.encode("utf-8")
    except UnicodeError:
        raise QualificationImportError("qualification_report_invalid_json") from None
    if len(report_bytes) > MAX_REPORT_BYTES:
        raise QualificationImportError("qualification_report_too_large")
    try:
        payload = parse_output(report_json)
    except Exception:
        raise QualificationImportError("qualification_report_invalid_json") from None
    from app.providers.effective_capabilities import resolve_effective_capabilities
    current = resolve_effective_capabilities().public()
    levels = {}
    for level in LEVEL_CASES:
        errors = validate_qualification_report(payload, current_identity=current, level=level)
        levels[level] = {"qualified": not errors, "status": "FAIL" if errors else "PASS", "reasons": errors[:64]}
    if not any(value["qualified"] for value in levels.values()):
        raise QualificationImportError("qualification_report_ineligible")
    run_id, finished = payload.get("run_id"), payload.get("finished_at")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id) or not _timestamp(finished):
        raise QualificationImportError("qualification_report_invalid_identity")
    # Recheck after parsing/validation; never bind a report to a switched config.
    final = resolve_effective_capabilities().public()
    if any(final.get(name) != current.get(name) for name in IDENTITY_FIELDS) or final.get("observation_stale") is not False:
        raise QualificationImportError("qualification_identity_changed")
    digest = hashlib.sha256(report_bytes).hexdigest()
    summary = {"status": "PASS" if levels["file_agent"]["qualified"] else "PARTIAL", "protocol": PROTOCOL,
               "target_version": TARGET_VERSION, "run_id": run_id, "finished_at": finished,
               "trust": TRUST, "report_sha256": digest, "imported_at": now_iso(), "levels": levels}
    stored = {"identity": {key: current[key] for key in IDENTITY_FIELDS}, "summary": summary}
    if len(json.dumps(stored, ensure_ascii=False).encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise QualificationImportError("qualification_summary_too_large")
    directory = _directory()
    if directory.is_symlink():
        raise QualificationImportError("qualification_store_unavailable")
    directory.mkdir(parents=True, exist_ok=True)
    # Content-addressed history is retained. Only the current small pointer is
    # replaced by an explicit import, so older evidence is not silently lost.
    _atomic_json(directory / f"{digest}.report.json", payload)
    _atomic_json(directory / f"{current['configuration_hash']}.json", stored)
    return summary
