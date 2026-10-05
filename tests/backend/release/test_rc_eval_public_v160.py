"""Synthetic public Eval projections; never execute a model or acceptance task."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import zipfile

import pytest

from app.evals.comparison import compare_reports, evaluate_gate
from app.evals.models import EvalReport, EvalTaskResult, Evidence, GatePolicy
from app.evals.reporting import aggregate_metrics

ROOT = Path(__file__).resolve().parents[3]
COMMIT = "a" * 40


def module(name):
    spec = importlib.util.spec_from_file_location("eval_public_test_" + name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(value)
    return value


public = module("rc_eval_public")
collector = module("record-rc-eval-public")
exporter = module("export-accepted-rc")
gate = module("rc_gate")
importer = module("import-accepted-rc")


def source(commit=COMMIT):
    return {"source_version": "16.0.0", "source_commit": commit, "workspace_clean": True,
            "source_tree_fingerprint": "b" * 64}


def projector():
    return {"generated_at": "2026-10-05T12:00:00+00:00", "source": source("d" * 40),
            "source_after": source("d" * 40), "module_sha256": "e" * 64, "collector_sha256": "f" * 64}


def raw_report(run_id="candidate", duration=100, commit=COMMIT):
    metrics = {name: 0 for name in ("model_calls", "tool_calls", "tool_errors", "retry_count", "total_tokens",
                "human_interventions", "tests_run", "tests_passed", "builds_run", "builds_passed",
                "permission_violations", "sandbox_violations")}
    result = EvalTaskResult(task_id="required-case", title="private label C:/workspace/title", status="passed",
        expected_outcome="passed", expectation_met=True, runtime_status="completed", started_at="2026-10-05T10:00:00+00:00",
        finished_at="2026-10-05T10:00:01+00:00", duration_ms=duration, metrics=metrics,
        evidence=[Evidence(rule="runtime_status", passed=True, message="private C:/workspace/message", data={"argv": ["C:/workspace/source"]})],
        trace={"tasks": [{"workspace": "C:/workspace/private", "prompt": "private prompt"}], "api_key": "sk-test_DO_NOT_USE_000000000000"})
    environment = {"schema_version": 1, "host": "c" * 64, "os": "Windows-10-10.0.22631-SP0", "machine": "AMD64",
        "python": [3, 12, 14], "workflow": "isolated-sqlite-runtime-v1",
        "runtime_limits": {name: 8 for name in ("max_duplicate_tool_calls", "max_phase_tokens", "max_model_call_tokens",
            "max_tool_result_chars", "max_file_snippet_chars", "max_consecutive_failures", "max_no_progress_rounds", "max_repair_attempts")}}
    configuration = {"task_count": 1, "permission_modes": ["agent"], "max_duplicate_tool_calls": 3,
        "database_isolated": True, "evaluation_contract": {"schema_version": 1, "suite": "core", "task_ids": ["required-case"], "tasks_sha256": "1" * 64},
        "comparison_environment": environment, "source_identity": source(commit), "source_identity_after": source(commit)}
    return EvalReport(run_id=run_id, label="private C:/workspace/label", app_version="16.0.0", mode="scripted_runtime", suite="core",
        provider={"name": "deterministic-script"}, configuration=configuration, started_at=result.started_at, finished_at=result.finished_at,
        duration_ms=duration, status="completed", metrics=aggregate_metrics([result]), task_results=[result],
        report_paths={"json": "C:/workspace/report.json", "markdown": "C:/workspace/report.md"}).model_dump(mode="json")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def projected(raw=None):
    return public.build_public_report(encoded(raw or raw_report()), projector=projector())


def put(root, name, block):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(block)
    return {"path": name, "bytes": len(block), "sha256": hashlib.sha256(block).hexdigest()}


def transport_fixture(root):
    for name in exporter.REQUIRED_FILES:
        put(root, name, b'{"synthetic_fixture":true}' if name.endswith(".json") else b"synthetic transport only")
    for kind, suffix in (("nsis", "-setup.exe"), ("msi", ".msi")):
        current = put(root, f"desktop/src-tauri/target/release/bundle/{kind}/synthetic16{suffix}", b"synthetic-current-" + kind.encode())
        previous = put(root, f"build/upgrade-baseline/synthetic8{suffix}", b"synthetic-previous-" + kind.encode())
        raw = {"synthetic_fixture": True, "artifacts": {key: {"name": Path(item["path"]).name, "bytes": item["bytes"], "sha256": item["sha256"]}
            for key, item in (("candidate", current), ("previous", previous))}}
        put(root, f"build/v1600-evidence/{kind}-installer-smoke.json", encoded(raw))


def test_public_projection_does_not_mutate_or_claim_a_new_measurement(tmp_path):
    raw = raw_report()
    original = encoded(raw)
    before = deepcopy(raw)
    result = public.build_public_report(original, projector=projector())
    assert raw == before
    assert result["private_origin"] == {"report_sha256": hashlib.sha256(original).hexdigest(), "report_bytes": len(original)}
    assert result["projection"] == projector()
    assert result["measurement"]["configuration"]["source_identity"] == source()
    assert result["measurement"]["run_id"] == raw["run_id"]
    assert result["measurement"]["started_at"] == raw["started_at"]
    assert result["measurement"]["metrics"] == raw["metrics"]
    assert result["measurement"]["task_results"][0]["metrics"] == raw["task_results"][0]["metrics"]
    assert "actual_run" not in result and "rc_eligible" not in result
    text = encoded(result)
    assert all(marker not in text for marker in (b"D:" + b"/", b"C:" + b"/", b"private prompt", b"private label", b"api_key", b"report_paths", b"argv"))
    public.validate_public_report(result)
    exporter.public_content("build/v1600-evidence/accepted/test.public.json", text)
    with pytest.raises(ValueError):
        exporter.public_content("build/v1600-evidence/accepted/raw.json", original)
    assert list(exporter.references(result)) == []


def test_projection_retains_original_average_time_regression():
    baseline_raw, candidate_raw = raw_report("baseline", 100, "2" * 40), raw_report("candidate", 117)
    original = compare_reports(EvalReport.model_validate(baseline_raw), EvalReport.model_validate(candidate_raw))
    baseline, candidate = public.as_evaluation_view(projected(baseline_raw)), public.as_evaluation_view(projected(candidate_raw))
    result = compare_reports(baseline, candidate)
    for name in ("comparable", "compatibility_errors", "metric_deltas", "regressions", "has_regressions"):
        assert result[name] == original[name]
    assert result["has_regressions"] is True
    assert not evaluate_gate(candidate, GatePolicy(), result)["passed"]
    assert any(item["metric"] == "average_task_time_ms" for item in result["regressions"])


def test_original_source_fingerprint_case_is_preserved_not_normalized():
    raw = raw_report()
    for field in ("source_identity", "source_identity_after"):
        raw["configuration"][field]["source_tree_fingerprint"] = "B" * 64
    result = projected(raw)
    assert result["measurement"]["configuration"]["source_identity"] == raw["configuration"]["source_identity"]
    assert public.as_evaluation_view(result).configuration["source_identity"]["source_tree_fingerprint"] == "B" * 64


@pytest.mark.parametrize("mutation", ["unknown", "hidden_ref", "actual_run", "source_after", "projector_after", "aggregate_none", "aggregate_bool",
    "aggregate_nan", "case_bool", "duration_none", "duration_bool", "duration_negative", "provider_url", "provider_unknown", "source_bool",
    "metric_unknown", "configuration_unknown", "environment_unknown", "duplicate_case", "missing_case", "private_path", "traversal", "rule_bool", "private_hash", "bytes_bool"])
def test_public_schema_rejects_type_abuse_and_unknown_or_private_fields(mutation):
    value = projected()
    measurement = value["measurement"]
    case = measurement["task_results"][0]
    if mutation == "unknown": value["unknown"] = True
    elif mutation == "hidden_ref": value["private_origin"]["hidden"] = {"path": "private/secret.json", "sha256": "0" * 64}
    elif mutation == "actual_run": value["actual_run"] = True
    elif mutation == "source_after": measurement["configuration"]["source_identity_after"]["source_commit"] = "0" * 40
    elif mutation == "projector_after": value["projection"]["source_after"]["source_commit"] = "0" * 40
    elif mutation.startswith("aggregate_"):
        measurement["metrics"]["task_count"] = {"aggregate_none": None, "aggregate_bool": True, "aggregate_nan": math.nan}[mutation]
    elif mutation == "case_bool": case["metrics"]["model_calls"] = True
    elif mutation.startswith("duration_"):
        case["duration_ms"] = {"duration_none": None, "duration_bool": True, "duration_negative": -1}[mutation]
    elif mutation == "provider_url": measurement["provider"] = {"endpoint_sha256": "f" * 64, "model": "safe-model", "url": "https://user:password@example.invalid"}
    elif mutation == "provider_unknown": measurement["provider"]["token"] = "private"
    elif mutation == "source_bool": measurement["configuration"]["source_identity"]["workspace_clean"] = 1
    elif mutation == "metric_unknown": case["metrics"]["secret"] = 0
    elif mutation == "configuration_unknown": measurement["configuration"]["hidden"] = {"path": "private/data", "sha256": "f" * 64}
    elif mutation == "environment_unknown": measurement["configuration"]["comparison_environment"]["hostname"] = "private"
    elif mutation == "duplicate_case": measurement["task_results"].append(deepcopy(case))
    elif mutation == "missing_case": measurement["task_results"] = []
    elif mutation == "private_path": case["changed_files"] = ["C:/workspace/file.txt"]
    elif mutation == "traversal": case["unrelated_files"] = ["../escape.txt"]
    elif mutation == "rule_bool": case["evidence"][0]["passed"] = 1
    elif mutation == "private_hash": value["private_origin"]["report_sha256"] = "not-a-hash"
    else: value["private_origin"]["report_bytes"] = True
    with pytest.raises(ValueError): public.validate_public_report(value)


@pytest.mark.parametrize("mutation", ["schema_bool", "case_bool", "none", "nan", "extra", "source_after", "aggregate", "duplicate"])
def test_builder_rejects_malformed_original_without_coercion(mutation):
    raw = raw_report()
    if mutation == "schema_bool": raw["schema_version"] = True
    elif mutation == "case_bool": raw["task_results"][0]["duration_ms"] = True
    elif mutation == "none": raw["metrics"]["task_count"] = None
    elif mutation == "nan": raw["metrics"]["task_count"] = math.nan
    elif mutation == "extra": raw["unknown"] = True
    elif mutation == "source_after": raw["configuration"]["source_identity_after"]["source_commit"] = "0" * 40
    elif mutation == "aggregate": raw["metrics"]["average_task_time_ms"] = 0
    else: raw["task_results"].append(deepcopy(raw["task_results"][0]))
    with pytest.raises(ValueError): projected(raw)


def test_duplicate_json_keys_and_bounded_reader_are_rejected(monkeypatch):
    raw = encoded(raw_report())
    duplicate = raw.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1', 1)
    with pytest.raises(ValueError): public.build_public_report(duplicate, projector=projector())
    monkeypatch.setattr(public, "MAX_REPORT_BYTES", 10)
    with pytest.raises(ValueError): public.build_public_report(raw, projector=projector())


@pytest.mark.parametrize("field", ["duration_ms", "metrics", "case"])
def test_extreme_json_integer_is_rejected_without_private_traceback(field):
    value = projected()
    if field == "metrics":
        value["measurement"]["metrics"]["task_count"] = 10**400
    elif field == "case":
        value["measurement"]["task_results"][0]["metrics"]["model_calls"] = 10**400
    else:
        value["measurement"][field] = 10**400
    with pytest.raises(public.PublicEvalError, match="finite numeric bound"):
        public.validate_public_report(value)


def test_safe_writer_is_exclusive_and_private_input_stays_identical(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    private = tmp_path / "private-original.json"
    original = encoded(raw_report())
    private.write_bytes(original)
    output = root / "build/v1600-evidence/accepted/evals/core/candidate.public.json"
    result = public.write_public_report(root, output, private, projector=projector())
    assert json.loads(output.read_bytes()) == result
    assert private.read_bytes() == original
    with pytest.raises(ValueError): public.write_public_report(root, output, private, projector=projector())
    assert private.read_bytes() == original


def test_writer_rejects_hardlinked_input_and_output_outside_generated_root(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    private = tmp_path / "private-original.json"
    private.write_bytes(encoded(raw_report()))
    linked = tmp_path / "linked.json"
    os.link(private, linked)
    output = root / "build/v1600-evidence/accepted/evals/core/candidate.public.json"
    with pytest.raises(ValueError): public.write_public_report(root, output, linked, projector=projector())
    with pytest.raises(ValueError): public.write_public_report(root, tmp_path / "outside.json", private, projector=projector())
    assert not output.exists()


def test_writer_rejects_reparse_ancestors_without_writing(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    private = tmp_path / "original.json"
    private.write_bytes(encoded(raw_report()))
    output = root / "build/v1600-evidence/accepted/evals/core/candidate.public.json"
    transport = public.module("export-accepted-rc")
    original = transport.no_links
    def denied(path):
        if path == output: raise ValueError("test-owned reparse point")
        return original(path)
    monkeypatch.setattr(transport, "no_links", denied)
    with pytest.raises(ValueError): public.write_public_report(root, output, private, projector=projector())
    assert not output.exists()


def test_writer_detects_changed_private_original(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    private = tmp_path / "original.json"
    private.write_bytes(encoded(raw_report()))
    output = root / "build/v1600-evidence/accepted/evals/core/candidate.public.json"
    original = public.build_public_report
    def changed(block, **kwargs):
        result = original(block, **kwargs)
        private.write_bytes(encoded(raw_report("replacement")))
        return result
    monkeypatch.setattr(public, "build_public_report", changed)
    with pytest.raises(ValueError): public.write_public_report(root, output, private, projector=projector())
    assert not output.exists()


def test_gate_consumes_typed_projection_and_keeps_source_binding(tmp_path, monkeypatch):
    baseline, candidate = projected(raw_report("baseline", 100, "2" * 40)), projected(raw_report("candidate", 100))
    references = {}
    for role, value in (("baseline", baseline), ("candidate", candidate)):
        entry = put(tmp_path, f"build/v1600-evidence/accepted/evals/core/{role}.public.json", encoded(value))
        references[role] = {key: entry[key] for key in ("path", "sha256")}
    from app.evals import loader
    from app.evals import contracts
    from app.evals import comparison
    monkeypatch.setattr(loader, "load_tasks", lambda *args, **kwargs: [])
    monkeypatch.setattr(contracts, "task_contract", lambda *args: candidate["measurement"]["configuration"]["evaluation_contract"])
    monkeypatch.setattr(comparison, "load_policy", lambda *args: GatePolicy())
    gate.evaluation_pair(tmp_path, references, "core", source())
    with pytest.raises(ValueError): gate.evaluation_pair(tmp_path, references, "core", source("3" * 40))


def test_transport_roundtrip_has_public_facts_not_private_original(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    transport_fixture(root)
    public_ref = put(root, "build/v1600-evidence/accepted/evals/core/candidate.public.json", encoded(projected()))
    put(root, "build/v1600-evidence/accepted/rc-bundle.json", encoded({"synthetic_fixture": True, "evaluation": {key: public_ref[key] for key in ("path", "sha256")}}))
    index = exporter.index_from_closure(root, COMMIT)
    assert public_ref in index["files"]
    assert not any("private" in item["path"] for item in index["files"])
    archive = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("artifact-index.json", encoded(index))
        for entry in index["files"]: stream.write(root / entry["path"], entry["path"])
    destination = tmp_path / "destination"
    destination.mkdir()
    received = destination / "build/v1600-evidence/rc-input"
    assert exporter.receive_archive(destination, archive, received, COMMIT)["status"] == "TRANSPORT_VALIDATED_NOT_RC_ACCEPTED"
    assert importer.plan(received, destination, COMMIT)
    restored = json.loads((received / public_ref["path"]).read_bytes())
    public.validate_public_report(restored)
    assert restored == projected()


@pytest.mark.parametrize("phase", ["index", "receive", "expanded"])
@pytest.mark.parametrize("mutation", ["unknown", "hidden_ref", "remove_protocol", "wrong_protocol", "metrics"])
def test_rehashed_transport_cannot_bypass_public_eval_schema(tmp_path, mutation, phase):
    root = tmp_path / "source"
    root.mkdir()
    transport_fixture(root)
    value = projected()
    name = "build/v1600-evidence/accepted/evals/core/candidate.public.json"
    valid = put(root, name, encoded(value))
    put(root, "build/v1600-evidence/accepted/rc-bundle.json", encoded({"synthetic_fixture": True, "evaluation": {key: valid[key] for key in ("path", "sha256")}}))
    index = exporter.index_from_closure(root, COMMIT)
    if mutation == "unknown": value["unknown"] = True
    elif mutation == "hidden_ref": value["private_origin"]["path"] = "private/secret.json"
    elif mutation == "remove_protocol": value.pop("public_protocol")
    elif mutation == "wrong_protocol": value["public_protocol"] = "installed-public-v1"
    else: value["measurement"]["metrics"]["average_task_time_ms"] = 0
    entry = put(root, name, encoded(value))
    put(root, "build/v1600-evidence/accepted/rc-bundle.json", encoded({"synthetic_fixture": True, "evaluation": {key: entry[key] for key in ("path", "sha256")}}))
    if phase == "index":
        with pytest.raises(ValueError): exporter.index_from_closure(root, COMMIT)
        return
    # Rehash every parent and archive entry; schema checks remain independent.
    for indexed in index["files"]:
        block = (root / indexed["path"]).read_bytes()
        indexed.update(bytes=len(block), sha256=hashlib.sha256(block).hexdigest())
    archive = tmp_path / "forged.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("artifact-index.json", encoded(index))
        for indexed in index["files"]: stream.write(root / indexed["path"], indexed["path"])
    destination = tmp_path / "destination"
    destination.mkdir()
    received = destination / "build/v1600-evidence/rc-input"
    if phase == "receive":
        with pytest.raises(ValueError): exporter.receive_archive(destination, archive, received, COMMIT)
    else:
        with zipfile.ZipFile(archive) as stream: stream.extractall(received)
        with pytest.raises(ValueError): exporter.validate_directory(destination, received, COMMIT)


def test_transport_rejects_duplicate_public_json_keys(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    transport_fixture(root)
    block = encoded(projected()).replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1', 1)
    entry = put(root, "build/v1600-evidence/accepted/evals/core/candidate.public.json", block)
    put(root, "build/v1600-evidence/accepted/rc-bundle.json", encoded({"synthetic_fixture": True, "evaluation": {key: entry[key] for key in ("path", "sha256")}}))
    with pytest.raises(ValueError): exporter.index_from_closure(root, COMMIT)


def test_public_model_and_relative_filename_do_not_bypass_privacy_scanner():
    value = projected()
    value["measurement"]["mode"] = "live_model"
    value["measurement"]["layer"] = "autonomous_model"
    value["measurement"]["provider"] = {"endpoint_sha256": "f" * 64, "model": "user.person@example.invalid"}
    with pytest.raises(ValueError): public.validate_public_report(value)
    value = projected()
    value["measurement"]["task_results"][0]["changed_files"] = ["user.person@example.invalid" + ".txt"]
    with pytest.raises(ValueError): public.validate_public_report(value)


def test_nested_eval_marker_is_not_an_unvalidated_top_level_receipt():
    with pytest.raises(ValueError): list(exporter.references({"nested": projected()}))


def test_collector_records_projector_identity_not_measurement_identity(tmp_path, monkeypatch):
    private = tmp_path / "private-original.json"
    private.write_bytes(encoded(raw_report(commit="2" * 40)))
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(collector, "ROOT", root)
    monkeypatch.setattr(collector, "projector_identity", projector)
    monkeypatch.setattr(collector, "current_source_identity", lambda: projector()["source_after"])
    output = "build/v1600-evidence/accepted/evals/core/baseline.public.json"
    assert collector.main(["--report", str(private), "--output", output]) == 0
    value = json.loads((root / output).read_bytes())
    assert value["measurement"]["configuration"]["source_identity"] == source("2" * 40)
    assert value["projection"]["source"] == source("d" * 40)
    assert collector.main(["--report", str(private), "--output", output]) == 1


def script_performance_receipt():
    comparison = {"baseline": {"binaries": {"desktop": {"path": "build/old/desktop.exe"}}},
        "candidate": {"binaries": {"desktop": {"path": "build/new/desktop.exe"}}, "startup_path": "portable-desktop", "cache_state": "warm"},
        "output": "build/v1600-evidence/performance.json"}
    return {"command_protocol": "python-script-argv-v1", "command": gate.performance_collection_arguments(comparison), "cwd": ".",
        "performance_comparison": comparison, "interpreter": {"implementation": "cpython", "version": [3, 12, 14],
            "launcher_sha256": "a" * 64, "runtime_sha256": "b" * 64}, "process_argv_sha256": "c" * 64}


def test_desktop_performance_script_argv_is_distinct_from_old_native_argv():
    receipt = script_performance_receipt()
    gate.automated_command(receipt, "startup_performance_comparison")
    native = deepcopy(receipt)
    for field in ("command_protocol", "interpreter", "process_argv_sha256"):
        native.pop(field)
    native["command"] = ["python.exe", *receipt["command"]]
    gate.automated_command(native, "startup_performance_comparison")
    with pytest.raises(ValueError): gate.automated_command({**native, "command": receipt["command"]}, "startup_performance_comparison")


@pytest.mark.parametrize("mutation", ["other_gate", "protocol", "none_protocol", "command", "cwd", "missing_interpreter", "extra_interpreter",
    "implementation", "version", "bool_version", "launcher", "runtime", "process", "bool_digest"])
def test_typed_performance_argv_cannot_bypass_other_commands_or_interpreter_checks(mutation):
    receipt = script_performance_receipt()
    requirement = "startup_performance_comparison"
    if mutation == "other_gate": requirement = "version_consistency"
    elif mutation == "protocol": receipt["command_protocol"] = "unknown"
    elif mutation == "none_protocol": receipt["command_protocol"] = None
    elif mutation == "command": receipt["command"] = ["python.exe", "-c", "scripts/record-rc-performance.py"]
    elif mutation == "cwd": receipt["cwd"] = "siyi"
    elif mutation == "missing_interpreter": receipt.pop("interpreter")
    elif mutation == "extra_interpreter": receipt["interpreter"]["path"] = "C:/workspace/python.exe"
    elif mutation == "implementation": receipt["interpreter"]["implementation"] = "pypy"
    elif mutation == "version": receipt["interpreter"]["version"] = [3, 13, 0]
    elif mutation == "bool_version": receipt["interpreter"]["version"] = [3, 12, True]
    elif mutation == "launcher": receipt["interpreter"]["launcher_sha256"] = "A" * 64
    elif mutation == "runtime": receipt["interpreter"]["runtime_sha256"] = None
    elif mutation == "process": receipt["process_argv_sha256"] = "not-a-hash"
    else: receipt["interpreter"]["launcher_sha256"] = True
    with pytest.raises(ValueError): gate.automated_command(receipt, requirement)
