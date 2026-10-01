import math
import asyncio

import pytest

from app.evals.comparison import compare_reports, evaluate_gate
from app.evals.models import EvalReport, EvalTaskResult, GatePolicy
from app.evals.reporting import aggregate_metrics


def report(run_id="candidate"):
    results = [EvalTaskResult(task_id="required-case", title="required", status="passed", expected_outcome="passed", expectation_met=True, started_at="2026-09-30", finished_at="2026-09-30", duration_ms=10)]
    return EvalReport(run_id=run_id, label=run_id, app_version="16.0.0", mode="scripted_runtime", suite="core", provider={"name": "deterministic-script"}, configuration={"evaluation_contract": {"schema_version": 1, "suite": "core", "task_ids": ["required-case"], "tasks_sha256": "a" * 64}, "comparison_environment": {"schema_version": 1, "host": "synthetic-test-only"}}, started_at="2026-09-30", finished_at="2026-09-30", duration_ms=10, status="completed", metrics=aggregate_metrics(results), task_results=results)


def test_no_baseline_is_not_no_regression():
    result = evaluate_gate(report(), GatePolicy())
    assert result["passed"] is False
    assert result["regression_status"] == "not_verified"


@pytest.mark.parametrize("mutation", ["mode", "layer", "suite", "provider", "missing_case", "duplicate_case", "contract", "environment", "legacy"])
def test_incompatible_reports_are_not_comparable(mutation):
    baseline, candidate = report("baseline"), report()
    if mutation == "mode":
        candidate.mode, candidate.layer = "live_model", "autonomous_model"
    elif mutation == "layer":
        candidate.layer = "autonomous_model"
    elif mutation == "suite":
        candidate.suite = "another-suite"
    elif mutation == "provider":
        candidate.provider = {"name": "different"}
    elif mutation == "missing_case":
        candidate.task_results = []
    elif mutation == "duplicate_case":
        candidate.task_results *= 2
    elif mutation == "contract":
        candidate.configuration["evaluation_contract"]["tasks_sha256"] = "b" * 64
    elif mutation == "environment":
        candidate.configuration["comparison_environment"]["host"] = "other"
    else:
        candidate.configuration.clear()
    comparison = compare_reports(baseline, candidate)
    assert comparison["comparable"] is False
    assert evaluate_gate(candidate, GatePolicy(), comparison)["passed"] is False


@pytest.mark.parametrize("value", [None, math.nan, math.inf, -1])
def test_unknown_or_invalid_safety_metric_cannot_pass(value):
    candidate = report()
    candidate.metrics["permission_violation_count"] = value
    assert evaluate_gate(candidate, GatePolicy(require_no_regressions=False))["passed"] is False


def test_aggregate_success_does_not_hide_missing_or_failed_required_case():
    candidate = report()
    candidate.task_results[0].expectation_met = False
    assert evaluate_gate(candidate, GatePolicy(require_no_regressions=False))["passed"] is False


@pytest.mark.parametrize("value", [math.nan, math.inf, -1, True])
def test_malformed_case_metric_is_rejected_without_crashing(value):
    candidate = report()
    candidate.task_results[0].metrics["total_tokens"] = value
    result = evaluate_gate(candidate, GatePolicy(require_no_regressions=False))
    assert result["passed"] is False
    assert compare_reports(report("baseline"), candidate)["comparable"] is False


def test_comparison_cannot_be_reused_for_a_different_candidate():
    comparison = compare_reports(report("baseline"), report("other"))
    assert evaluate_gate(report(), GatePolicy(), comparison)["passed"] is False


def test_matching_distinct_runs_can_pass_evaluation_gate():
    candidate = report()
    comparison = compare_reports(report("baseline"), candidate)
    assert evaluate_gate(candidate, GatePolicy(), comparison)["passed"] is True


@pytest.mark.parametrize("kind", ["mode", "version", "contract", "self_baseline"])
def test_release_requirements_cannot_be_replaced_by_report_declarations(kind):
    candidate = report()
    baseline = report("baseline")
    kwargs = {"expected_contract": candidate.configuration["evaluation_contract"], "expected_mode": candidate.mode, "expected_version": candidate.app_version}
    if kind == "mode":
        kwargs["expected_mode"] = "live_model"
    elif kind == "version":
        kwargs["expected_version"] = "17.0.0"
    elif kind == "contract":
        kwargs["expected_contract"] = {**kwargs["expected_contract"], "task_ids": ["required-case", "missing-case"]}
    else:
        baseline.run_id = candidate.run_id
    assert not evaluate_gate(candidate, GatePolicy(), compare_reports(baseline, candidate), **kwargs)["passed"]


def test_provider_endpoint_in_report_is_not_secret_bearing(monkeypatch):
    from app.config import settings
    from app.evals.contracts import provider_identity

    monkeypatch.setattr(settings, "model_base_url", "https://username:password@example.invalid/v1?key=secret")
    identity = provider_identity("live_model")
    assert "example.invalid" not in str(identity)
    assert "password" not in str(identity)
    assert "secret" not in str(identity)
    assert len(identity["endpoint_sha256"]) == 64


@pytest.mark.parametrize("changed", [False, True])
def test_source_capture_marks_mid_run_source_changes_invalid(tmp_path, monkeypatch, changed):
    from app.evals import source_identity
    from app.evals.runner import run_evaluation

    before = {"source_version": "16.0.0", "source_commit": "a" * 40, "workspace_clean": False, "source_tree_fingerprint": "b" * 64}
    after = {**before, "source_tree_fingerprint": ("c" if changed else "b") * 64}
    values = iter((before, after))
    monkeypatch.setattr(source_identity, "repository_identity", lambda: next(values))
    result = asyncio.run(run_evaluation(label="test-source-binding", task_ids=["project-structure"], output_root=tmp_path, capture_source=True))
    assert result.configuration["source_identity"] == before
    assert result.configuration["source_identity_after"] == after
    assert result.status == ("invalid" if changed else "completed")
