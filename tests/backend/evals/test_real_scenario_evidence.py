from __future__ import annotations

import json
from pathlib import Path

from app.evals.real_scenarios import validate_catalog, validate_scenario_directory


def test_real_scenario_catalog_covers_all_eighteen_scenarios() -> None:
    root = Path(__file__).resolve().parents[3]
    assert validate_catalog(root / "real-scenarios" / "catalog.json") == []


def _write_required_bundle(root: Path, payload: dict) -> None:
    root.mkdir()
    (root / "scenario.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "tool-receipts.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "token-ledger.json").write_text("{}\n", encoding="utf-8")
    (root / "final-report.md").write_text("# Report\n", encoding="utf-8")
    (root / "screenshots").mkdir()
    (root / "artifacts").mkdir()


def test_pass_requires_real_command_build_and_existing_evidence(tmp_path: Path) -> None:
    scenario = tmp_path / "RS-001"
    _write_required_bundle(
        scenario,
        {
            "scenario_id": "RS-001",
            "status": "PASS",
            "started_at": "2026-07-26T00:00:00Z",
            "finished_at": "2026-07-26T00:01:00Z",
            "build": {},
            "commands": [],
            "evidence_files": [],
        },
    )
    errors = validate_scenario_directory(scenario)
    assert any("complete build identity" in error for error in errors)
    assert any("executed command" in error for error in errors)
    assert any("requires evidence_files" in error for error in errors)


def test_pass_accepts_bound_runtime_evidence(tmp_path: Path) -> None:
    scenario = tmp_path / "RS-001"
    _write_required_bundle(
        scenario,
        {
            "scenario_id": "RS-001",
            "status": "PASS",
            "started_at": "2026-07-26T00:00:00Z",
            "finished_at": "2026-07-26T00:01:00Z",
            "build": {
                "product_version": "8.0.0",
                "build_id": "test-build",
                "source_fingerprint": "test-source",
            },
            "commands": [
                {
                    "command": "pytest scenario",
                    "started_at": "2026-07-26T00:00:10Z",
                    "finished_at": "2026-07-26T00:00:20Z",
                    "exit_code": 0,
                }
            ],
            "evidence_files": ["artifacts/result.json"],
        },
    )
    (scenario / "artifacts" / "result.json").write_text("{}\n", encoding="utf-8")
    assert validate_scenario_directory(scenario) == []
