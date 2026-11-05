"""Original RC consumer regressions with synthetic, source-bound Eval reports.

No model/installer is executed. These fixtures prove rejection/acceptance of
cross-report identity binding, not genuine model capability or formal RC.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from app.evals import local_live
from app.evals.contracts import comparison_environment, task_contract
from app.evals.loader import load_tasks
from app.evals.models import EvalReport, EvalTaskResult
from app.evals.reporting import aggregate_metrics
from app.providers.capabilities import provider_identity
from app.providers.configuration import ProviderConfiguration
from app.providers.effective_capabilities import configuration_identity

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("rc_local_binding_gate", ROOT / "scripts/rc_gate.py")
gate = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(gate)
CONFIG = ProviderConfiguration(provider_id="ollama", base_url="http://127.0.0.1:11434", model="qwen3:4b", max_retries=0)
SOURCE = {"source_version": "16.0.1", "source_commit": "a" * 40, "workspace_clean": True, "source_tree_fingerprint": "B" * 64}


def designated():
    return {"provider_id": "ollama", "endpoint_hash": provider_identity(CONFIG.base_url)[1], "model": CONFIG.model,
        "configuration_hash": configuration_identity(CONFIG), "identity_hash": "a" * 64, "model_digest": "b" * 64,
        "deployment_hash": "c" * 64, "context_window_tokens": 16384, "runtime_context_window": 16384,
        "local": True, "observation_stale": False, "declared_capabilities": {"native_tool_calls": True},
        "observed_capabilities": {"native_tool_calls": "supported"}, "explicit_profile": {"context_window_tokens": 16384}}


def core_provider(changes=None):
    current = designated()
    observed = {key: current[key] for key in local_live.SNAPSHOT_FIELDS - {"endpoint_sha256", "capabilities_sha256"}}
    observed.update(endpoint_sha256=local_live._endpoint_digest(CONFIG), capabilities_sha256=local_live.capabilities_digest(current))
    observed.update(changes or {})
    return local_live.public_provider(CONFIG, "f" * 64, 16384, deepcopy(observed), deepcopy(observed))


def report(run_id, provider):
    tasks = load_tasks(ROOT / "evals/tasks.json", suite="core")
    metrics = {key: 0 for key in ("model_calls", "tool_calls", "tool_errors", "retry_count", "total_tokens",
        "human_interventions", "tests_run", "tests_passed", "builds_run", "builds_passed", "permission_violations", "sandbox_violations")}
    metrics["model_calls"] = 1
    results = [EvalTaskResult(task_id=task.id, title="synthetic RC binding only", status=task.expected_outcome,
        expected_outcome=task.expected_outcome, expectation_met=True, runtime_status="completed",
        started_at="2026-11-05T00:00:00+00:00", finished_at="2026-11-05T00:00:01+00:00", duration_ms=10,
        metrics=dict(metrics)) for task in tasks]
    return EvalReport(run_id=run_id, label="synthetic binding fixture", app_version="16.0.1", mode="live_model",
        layer="autonomous_model", suite="core", provider=deepcopy(provider), configuration={
            "task_count": len(tasks), "permission_modes": sorted({task.permission_mode for task in tasks}),
            "max_duplicate_tool_calls": 3, "database_isolated": True, "evaluation_contract": task_contract(tasks, "core"),
            "comparison_environment": comparison_environment(), "source_identity": SOURCE, "source_identity_after": SOURCE},
        started_at="2026-11-05T00:00:00+00:00", finished_at="2026-11-05T00:00:01+00:00", duration_ms=180,
        status="completed", metrics=aggregate_metrics(results), task_results=results).model_dump(mode="json")


def pair(root, *, changes=None, strip=False, public=False, zero_calls=False):
    (root / "evals").mkdir()
    for name in ("tasks.json", "gate-policy.json"):
        shutil.copyfile(ROOT / "evals" / name, root / "evals" / name)
    provider = core_provider(changes)
    if strip:
        provider.pop("local_live")
    references = {}
    for role in ("baseline", "candidate"):
        value = report(role, provider)
        if zero_calls:
            for row in value["task_results"]:
                row["metrics"]["model_calls"] = 0
            from app.evals.models import EvalTaskResult
            value["metrics"] = aggregate_metrics([EvalTaskResult.model_validate(row) for row in value["task_results"]])
        if public:
            projector = {"generated_at": "2026-11-05T00:00:02+00:00", "source": SOURCE, "source_after": SOURCE,
                         "module_sha256": "e" * 64, "collector_sha256": "f" * 64}
            value = gate.module("rc_eval_public").build_public_report(json.dumps(value).encode(), projector=projector)
        output = root / (role + ".json")
        output.write_text(json.dumps(value), encoding="utf-8")
        references[role] = {"path": output.name, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    return references


@pytest.mark.parametrize("public", [False, True])
def test_original_consumer_accepts_matching_designated_identity(tmp_path, public):
    refs = pair(tmp_path, public=public)
    gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE, model_identity=designated())


@pytest.mark.parametrize("field,value", [
    ("model_digest", "d" * 64), ("deployment_hash", "d" * 64), ("configuration_hash", "d" * 64),
    ("identity_hash", "d" * 64), ("endpoint_hash", "d" * 16), ("capabilities_sha256", "d" * 64),
])
def test_self_consistent_core_pair_from_other_weights_listener_or_profile_is_rejected(tmp_path, field, value):
    refs = pair(tmp_path, changes={field: value})
    # It really is independently comparable. The new formal designated binding
    # is what stops qualification A plus matching-but-different core pair B.
    gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE)
    with pytest.raises(ValueError, match="designated_mismatch"):
        gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE, model_identity=designated())


def test_same_name_endpoint_but_different_resident_context_is_rejected(tmp_path):
    current = designated()
    current["context_window_tokens"] = current["runtime_context_window"] = 8192
    refs = pair(tmp_path)
    with pytest.raises(ValueError, match="designated_mismatch"):
        gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE, model_identity=current)


@pytest.mark.parametrize("field", ["provider_id", "model", "configuration_hash", "identity_hash", "model_digest", "deployment_hash",
    "context_window_tokens", "runtime_context_window", "declared_capabilities", "observed_capabilities", "explicit_profile"])
def test_missing_designated_fields_cannot_be_assumed_matching(tmp_path, field):
    current = designated()
    del current[field]
    refs = pair(tmp_path)
    with pytest.raises(ValueError):
        gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE, model_identity=current)


@pytest.mark.parametrize("field,value", [("local", False), ("observation_stale", True), ("runtime_context_window", True),
    ("observed_capabilities", {"native_tool_calls": "unsupported"}), ("explicit_profile", {"context_window_tokens": 8192})])
def test_stale_wrong_type_or_capability_designation_rejected(tmp_path, field, value):
    current = {**designated(), field: value}
    refs = pair(tmp_path)
    with pytest.raises(ValueError):
        gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE, model_identity=current)


@pytest.mark.parametrize("public", [False, True])
def test_stripping_local_metadata_cannot_downgrade_to_legacy(tmp_path, public):
    refs = pair(tmp_path, strip=True, public=public)
    gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE)
    with pytest.raises(ValueError, match="metadata_missing"):
        gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE, model_identity=designated())


def test_nonlocal_legacy_report_behavior_is_preserved(tmp_path):
    refs = pair(tmp_path, strip=True)
    gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE, model_identity={"provider_id": "deepseek"})


def test_actual_call_requirement_is_not_relaxed_by_matching_metadata(tmp_path):
    refs = pair(tmp_path, zero_calls=True)
    with pytest.raises(ValueError, match="actual autonomous-model calls"):
        gate.evaluation_pair(tmp_path, refs, "default_model", SOURCE, model_identity=designated())


@pytest.mark.parametrize("strip", [False, True])
def test_original_check_bundle_passes_designation_to_default_core_consumer(tmp_path, strip):
    refs = pair(tmp_path, strip=strip, public=True)
    result = gate.check_bundle(tmp_path, {"evaluations": {"default_model": refs}}, current=SOURCE,
        build_manifest={"build_id": "synthetic-only"}, model_identity=designated())
    core = next(row for row in result["results"] if row["requirement_id"] == "V160-Q01-EVAL-DEFAULT_MODEL")
    assert core["status"] == ("FAIL" if strip else "PASS")
    assert result["passed"] is False and result["status"] == "BLOCKED"
