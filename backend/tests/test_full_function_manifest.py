from pathlib import Path

from app.evals.full_function import EXPECTED_SEVERITY, EXPECTED_SMOKE, EXPECTED_TOTAL, assess_manifest, load_manifest, required_gates, valid_evidence


ROOT = Path(__file__).resolve().parents[2]


def test_targeted_manifest_is_complete_and_stable() -> None:
    manifest, cases = load_manifest(ROOT / "backend" / "evals" / "full_function_manifest_v2.json")
    assert len(cases) == EXPECTED_TOTAL
    assert len(manifest["smoke"]) == EXPECTED_SMOKE
    assert {severity: sum(case.severity == severity for case in cases) for severity in EXPECTED_SEVERITY} == EXPECTED_SEVERITY
    assert all(required_gates(case) for case in cases)


def test_product_decision_preserves_pause_and_resume_contract() -> None:
    _, cases = load_manifest(ROOT / "backend" / "evals" / "full_function_manifest_v2.json")
    pause = next(case for case in cases if case.id == "RUN-012")
    decision = (ROOT / "docs" / "acceptance" / "v4-targeted" / "PRODUCT_DECISIONS.md").read_text(encoding="utf-8")
    assert pause.severity == "P0"
    assert "保留暂停与恢复" in decision
    assert "RUN-012" in decision


def test_full_function_evidence_requires_traceable_build_artifact() -> None:
    complete = {
        "status": "passed",
        "evidence_type": "automated",
        "command": "pytest",
        "artifact": "build/test.log",
        "recorded_at": "2026-07-16T00:00:00Z",
        "build_id": "build-123",
    }
    assert valid_evidence(complete)
    assert not valid_evidence({"status": "passed"})


def test_generic_gates_do_not_replace_case_specific_scenario_evidence() -> None:
    evidence = {
        "status": "passed",
        "evidence_type": "automated",
        "command": "test",
        "artifact": "build/test.log",
        "recorded_at": "2026-07-16T00:00:00Z",
        "build_id": "build-123",
    }
    report = assess_manifest(
        ROOT / "backend" / "evals" / "full_function_manifest_v2.json",
        {name: dict(evidence) for name in ("backend", "frontend", "rust", "desktop", "release", "v4")},
    )
    assert report["release_ready"] is False
    assert report["summary"]["blocked"] > 0
    assert any("scenario" in item["missing_gates"] for item in report["results"])
