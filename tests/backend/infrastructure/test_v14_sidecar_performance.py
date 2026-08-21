from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "v14-sidecar-performance.py"
SPEC = importlib.util.spec_from_file_location("v14_sidecar_performance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def sample(readiness_ms: int, *, build_id: str = "candidate", fingerprint: str = "fingerprint") -> dict[str, object]:
    return {
        "readiness_ms": readiness_ms,
        "version": "13.0.0",
        "build_id": build_id,
        "source_fingerprint": fingerprint,
        "workspace_state": "DIRTY",
    }


def test_v14_sidecar_contract_requires_three_median_ten_p95_and_bounded_drift() -> None:
    decision = MODULE.evaluate(
        candidate_median_samples=[sample(1_550), sample(1_560), sample(1_570)],
        candidate_p95_samples=[sample(value) for value in (1_500, 1_510, 1_520, 1_530, 1_540, 1_550, 1_560, 1_570, 1_580, 1_600)],
        baseline_samples=[sample(1_500, build_id="baseline"), sample(1_510, build_id="baseline"), sample(1_520, build_id="baseline")],
    )
    assert decision["candidate_median_ms"] == 1_560
    assert decision["candidate_p95_ms"] == 1_600
    assert decision["baseline_median_ms"] == 1_510
    assert decision["status"] == "PASS"


def test_v14_sidecar_contract_rejects_p95_or_regression_failure() -> None:
    p95_failed = MODULE.evaluate(
        candidate_median_samples=[sample(1_600), sample(1_610), sample(1_620)],
        candidate_p95_samples=[sample(value) for value in (1_500, 1_510, 1_520, 1_530, 1_540, 1_550, 1_560, 1_570, 1_580, 3_100)],
        baseline_samples=[sample(1_500, build_id="baseline"), sample(1_510, build_id="baseline"), sample(1_520, build_id="baseline")],
    )
    assert p95_failed["checks"]["candidate_p95"] is False
    assert p95_failed["status"] == "FAIL"

    regression_failed = MODULE.evaluate(
        candidate_median_samples=[sample(1_700), sample(1_710), sample(1_720)],
        candidate_p95_samples=[sample(value) for value in (1_600, 1_610, 1_620, 1_630, 1_640, 1_650, 1_660, 1_670, 1_680, 1_690)],
        baseline_samples=[sample(1_500, build_id="baseline"), sample(1_510, build_id="baseline"), sample(1_520, build_id="baseline")],
    )
    assert regression_failed["checks"]["regression"] is False
    assert regression_failed["status"] == "FAIL"


def test_v14_sidecar_contract_rejects_inconsistent_or_wrong_sample_identity() -> None:
    with pytest.raises(MODULE.SidecarPerformanceError, match="inconsistent build_id"):
        MODULE.evaluate(
            candidate_median_samples=[sample(1_500), sample(1_510, build_id="other"), sample(1_520)],
            candidate_p95_samples=[sample(1_500) for _ in range(10)],
            baseline_samples=[sample(1_450, build_id="baseline") for _ in range(3)],
        )
    with pytest.raises(MODULE.SidecarPerformanceError, match="expected 10 P95"):
        MODULE.nearest_rank_p95_ms([sample(1_500) for _ in range(9)])


def test_v14_sidecar_output_is_confined_to_target_evidence_directory(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    (root / "build" / "v1400-evidence").mkdir(parents=True)
    monkeypatch.setattr(MODULE, "ROOT", root)
    assert MODULE.controlled_output("build/v1400-evidence/a22.json") == root / "build" / "v1400-evidence" / "a22.json"
    with pytest.raises(MODULE.SidecarPerformanceError, match="must remain"):
        MODULE.controlled_output("build/not-v14/a22.json")


def test_v14_sidecar_report_requires_a_real_onedir_support_payload(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    candidate = root / "candidate.exe"
    baseline = root / "baseline.exe"
    candidate.write_bytes(b"candidate")
    baseline.write_bytes(b"baseline")
    monkeypatch.setattr(MODULE, "ROOT", root)
    with pytest.raises(MODULE.SidecarPerformanceError, match="onedir sidecar"):
        MODULE.report(
            candidate=candidate,
            baseline=baseline,
            candidate_median_samples=[sample(1_500), sample(1_510), sample(1_520)],
            candidate_p95_samples=[sample(1_500) for _ in range(10)],
            baseline_samples=[sample(1_450, build_id="baseline") for _ in range(3)],
        )
