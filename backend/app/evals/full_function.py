from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXPECTED_TOTAL = 180
EXPECTED_SEVERITY = {"P0": 125, "P1": 54, "P2": 1}
EXPECTED_SMOKE = 30
EVIDENCE_FIELDS = ("status", "evidence_type", "command", "artifact", "recorded_at", "build_id")


@dataclass(frozen=True)
class FullFunctionCase:
    id: str
    module: str
    title: str
    severity: str
    mode: str
    source: str
    pre: str
    action: str
    expected: str
    evidence: str


def load_manifest(path: Path) -> tuple[dict[str, Any], list[FullFunctionCase]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("full-function manifest must contain a cases array")
    cases = [FullFunctionCase(**item) for item in raw_cases]
    ids = [item.id for item in cases]
    if len(cases) != EXPECTED_TOTAL or payload.get("total") != EXPECTED_TOTAL:
        raise ValueError(f"expected {EXPECTED_TOTAL} cases, found {len(cases)}")
    if len(set(ids)) != len(ids):
        raise ValueError("full-function manifest contains duplicate case ids")
    severity = {name: sum(item.severity == name for item in cases) for name in EXPECTED_SEVERITY}
    if severity != EXPECTED_SEVERITY:
        raise ValueError(f"severity distribution mismatch: {severity}")
    smoke = payload.get("smoke")
    if not isinstance(smoke, list) or len(smoke) != EXPECTED_SMOKE or not set(smoke).issubset(ids):
        raise ValueError("smoke set must contain 30 valid unique case ids")
    return payload, cases


def required_gates(case: FullFunctionCase) -> tuple[str, ...]:
    gates = {"backend"}
    if case.id.startswith(("UI-", "CHAT-")):
        gates.add("frontend")
    if case.id.startswith("DESK-"):
        gates.update(("frontend", "rust", "desktop"))
    if case.id.startswith("V4-"):
        gates.add("v4")
    if case.id.startswith(("SEC-014", "SEC-015", "SEC-016", "SEC-018", "OPS-009", "OPS-010")):
        gates.update(("desktop", "release"))
    if "E2E" in case.mode or "手动" in case.mode or "人工" in case.mode or "产品决策" in case.mode:
        gates.add("scenario")
    return tuple(sorted(gates))


def valid_evidence(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict) or item.get("status") not in {"passed", "failed"}:
        return False
    if item.get("evidence_type") not in {"automated", "e2e", "manual"}:
        return False
    return all(str(item.get(field) or "").strip() for field in EVIDENCE_FIELDS if field != "status")


def assess_manifest(
    manifest_path: Path,
    gate_results: dict[str, dict[str, Any]],
    scenario_results: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest, cases = load_manifest(manifest_path)
    scenario_results = scenario_results or {}
    results: list[dict[str, Any]] = []
    for case in cases:
        gates = required_gates(case)
        evidence: list[dict[str, Any]] = []
        missing: list[str] = []
        for gate in gates:
            item = scenario_results.get(case.id) if gate == "scenario" else gate_results.get(gate)
            if not valid_evidence(item) or item.get("status") != "passed":
                missing.append(gate)
            if item:
                evidence.append({"gate": gate, **item})
        status = "passed" if not missing else "failed" if any(
            valid_evidence(scenario_results.get(case.id) if gate == "scenario" else gate_results.get(gate))
            and (scenario_results.get(case.id) if gate == "scenario" else gate_results.get(gate) or {}).get("status") == "failed"
            for gate in missing
        ) else "blocked"
        results.append({
            "id": case.id,
            "module": case.module,
            "title": case.title,
            "severity": case.severity,
            "mode": case.mode,
            "status": status,
            "required_gates": gates,
            "missing_gates": missing,
            "evidence": evidence,
        })
    summary: dict[str, Any] = {"total": len(results)}
    for status in ("passed", "failed", "blocked"):
        summary[status] = sum(item["status"] == status for item in results)
    summary["by_severity"] = {
        severity: {
            status: sum(item["severity"] == severity and item["status"] == status for item in results)
            for status in ("passed", "failed", "blocked")
        }
        for severity in EXPECTED_SEVERITY
    }
    p0 = summary["by_severity"]["P0"]
    p1 = summary["by_severity"]["P1"]
    p2 = summary["by_severity"]["P2"]
    release_ready = (
        p0["passed"] == EXPECTED_SEVERITY["P0"]
        and p1["passed"] / EXPECTED_SEVERITY["P1"] >= 0.95
        and p2["passed"] / EXPECTED_SEVERITY["P2"] >= 0.95
        and summary["failed"] == 0
    )
    return {
        "suite": manifest["suite"],
        "source_commit": manifest["commit"],
        "summary": summary,
        "release_ready": release_ready,
        "results": results,
    }
