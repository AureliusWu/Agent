from pathlib import Path

from app.evals.full_function import EXPECTED_SEVERITY, EXPECTED_SMOKE, EXPECTED_TOTAL, load_manifest, required_gates


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
