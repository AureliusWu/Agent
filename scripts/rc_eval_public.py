"""Strict, path-free Eval facts; immutable private reports are never rewritten.

Projection is not a measurement, execution attestation, signature or RC verdict.
The original Eval v1 did not attest argv/actual_run; this protocol does not invent
either. Private report digests are commitments, not public attachment references.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from functools import lru_cache
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "eval-public-v1"
REPORT_TYPE = "rc_eval_public_evidence"
VERSION = "16.0.0"
MAX_REPORT_BYTES = 32 * 1024**2
MAX_CASES = 2048
SHA = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_FIELDS = {"source_version", "source_commit", "workspace_clean", "source_tree_fingerprint"}
TOP_FIELDS = {"schema_version", "report_type", "public_protocol", "target_version", "projection", "private_origin", "measurement"}
REPORT_FIELDS = {"schema_version", "run_id", "app_version", "mode", "layer", "suite", "provider", "configuration",
                 "started_at", "finished_at", "duration_ms", "status", "metrics", "task_results"}
CASE_FIELDS = {"task_id", "status", "expected_outcome", "expectation_met", "runtime_status", "started_at", "finished_at",
               "duration_ms", "failed_step", "evidence", "metrics", "changed_files", "unrelated_files", "false_success"}
CONFIGURATION_FIELDS = {"task_count", "permission_modes", "max_duplicate_tool_calls", "database_isolated", "evaluation_contract",
                        "comparison_environment", "source_identity", "source_identity_after"}
LIMITS = {"max_duplicate_tool_calls", "max_phase_tokens", "max_model_call_tokens", "max_tool_result_chars", "max_file_snippet_chars",
          "max_consecutive_failures", "max_no_progress_rounds", "max_repair_attempts"}
CASE_METRICS = {"model_calls", "tool_calls", "tool_errors", "retry_count", "total_tokens", "human_interventions", "tests_run",
                "tests_passed", "builds_run", "builds_passed", "permission_violations", "sandbox_violations"}
AGGREGATE_METRICS = {"task_count", "task_success_count", "task_success_rate", "partial_completion_rate", "false_success_count",
    "false_success_rate", "tool_error_rate", "retry_count", "model_call_count", "tool_call_count", "token_cost", "execution_time_ms",
    "average_task_time_ms", "human_intervention_count", "unrelated_file_modification_count", "test_pass_rate", "build_pass_rate",
    "permission_violation_count", "sandbox_violation_count", "recovery_success_rate"}
STATUSES = {"passed", "partially_passed", "failed", "blocked", "cancelled", "timed_out", "invalid"}
RULES = {"runtime_status", "file_exists", "file_contains", "file_not_contains", "tool_called", "tool_error", "command_succeeded",
    "sandbox_rejected", "no_file_changes", "no_unrelated_changes", "approval_count", "max_tool_calls", "recovery_succeeded",
    "sidecar_stopped", "timeout_and_cancelled", "mcp_failure_contained", "child_agent_count", "agent_role", "file_lock_recorded",
    "verifier_revision", "agent_profile", "prompt_injection_detected", "completion_not_claimed", "provider_key"}
MODE_LAYERS = {"scripted_runtime": "deterministic_runtime", "live_model": "autonomous_model", "adversarial": "adversarial"}


class PublicEvalError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PublicEvalError(message)


def exact(value, fields, label: str) -> None:
    require(isinstance(value, dict) and set(value) == set(fields), label + " has unknown or missing fields")


def text(value, label: str, *, limit: int = 512) -> None:
    require(isinstance(value, str) and 0 < len(value) <= limit and not any(ord(char) < 32 for char in value), label + " is invalid")


def number(value, label: str, *, integer: bool = False, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    require(type(value) is int if integer else type(value) in {int, float}, label + " is not a number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        raise PublicEvalError(label + " exceeds the finite numeric bound") from None
    require(finite and value >= 0, label + " is negative or non-finite")


def timestamp(value, label: str) -> None:
    text(value, label, limit=64)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise PublicEvalError(label + " is invalid") from None


def source_identity(value) -> None:
    exact(value, SOURCE_FIELDS, "source identity")
    require(isinstance(value["source_version"], str) and re.fullmatch(r"\d+\.\d+\.\d+", value["source_version"]), "source version is invalid")
    require(isinstance(value["source_commit"], str) and re.fullmatch(r"[0-9a-f]{40,64}", value["source_commit"]), "source commit is invalid")
    require(type(value["workspace_clean"]) is bool and isinstance(value["source_tree_fingerprint"], str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", value["source_tree_fingerprint"]), "source fingerprint or state is invalid")


@lru_cache(maxsize=4)
def module(name: str):
    specification = importlib.util.spec_from_file_location("rc_eval_public_" + name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    require(specification is not None and specification.loader is not None, "required validator unavailable")
    value = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = value
    specification.loader.exec_module(value)
    return value


def evaluation_types():
    directory = str(ROOT / "siyi")
    if directory not in sys.path:
        sys.path.insert(0, directory)
    from app.evals.models import EvalReport
    from app.evals.comparison import report_integrity_errors
    return EvalReport, report_integrity_errors


def _paths(value, label: str) -> None:
    require(isinstance(value, list) and len(value) <= 4096, label + " is invalid")
    for path in value:
        module("export-accepted-rc").canonical_path(path)
    require(len(value) == len(set(value)), label + " contains duplicate paths")


def _metrics(value, *, aggregate: bool) -> None:
    require(isinstance(value, dict), "metrics must be an object")
    if aggregate:
        exact(value, AGGREGATE_METRICS, "aggregate metrics")
    else:
        require(set(value) <= CASE_METRICS, "case metrics have unknown fields")
    for name, item in value.items():
        number(item, "metric " + name, nullable=not aggregate)


def _configuration(value, *, suite: str, case_ids: list[str]) -> None:
    exact(value, CONFIGURATION_FIELDS, "configuration")
    number(value["task_count"], "configured task count", integer=True)
    require(value["task_count"] == len(case_ids), "configured task count differs")
    modes = value["permission_modes"]
    require(isinstance(modes, list) and bool(modes) and len(modes) <= 3
            and all(type(mode) is str and mode in {"ask", "agent", "full"} for mode in modes)
            and modes == sorted(set(modes)), "permission modes are invalid")
    number(value["max_duplicate_tool_calls"], "duplicate tool limit", integer=True)
    require(type(value["database_isolated"]) is bool, "database isolation is unknown")
    contract = value["evaluation_contract"]
    exact(contract, {"schema_version", "suite", "task_ids", "tasks_sha256"}, "evaluation contract")
    require(type(contract["schema_version"]) is int and contract["schema_version"] == 1
            and contract["suite"] == suite and contract["task_ids"] == sorted(case_ids)
            and isinstance(contract["tasks_sha256"], str) and SHA.fullmatch(contract["tasks_sha256"]), "evaluation contract differs")
    environment = value["comparison_environment"]
    exact(environment, {"schema_version", "host", "os", "machine", "python", "workflow", "runtime_limits"}, "comparison environment")
    require(type(environment["schema_version"]) is int and environment["schema_version"] == 1
            and isinstance(environment["host"], str) and SHA.fullmatch(environment["host"]), "comparison environment identity is invalid")
    text(environment["os"], "OS", limit=256)
    text(environment["machine"], "machine", limit=64)
    require(environment["workflow"] == "isolated-sqlite-runtime-v1", "comparison workflow differs")
    python = environment["python"]
    require(isinstance(python, list) and len(python) == 3 and all(type(part) is int and 0 <= part < 1000 for part in python), "Python version is invalid")
    exact(environment["runtime_limits"], LIMITS, "runtime limits")
    for item in environment["runtime_limits"].values():
        number(item, "runtime limit")
    source_identity(value["source_identity"])
    source_identity(value["source_identity_after"])
    require(value["source_identity"] == value["source_identity_after"], "measurement source changed")


def _measurement(value) -> None:
    exact(value, REPORT_FIELDS, "public measurement")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, "Eval schema differs")
    text(value["run_id"], "run ID", limit=128)
    require(isinstance(value["app_version"], str) and re.fullmatch(r"\d+\.\d+\.\d+", value["app_version"]), "application version is invalid")
    mode = value["mode"]
    require(type(mode) is str and mode in MODE_LAYERS and value["layer"] == MODE_LAYERS[mode], "mode/layer differs")
    text(value["suite"], "suite", limit=64)
    provider = value["provider"]
    if mode == "live_model":
        exact(provider, {"endpoint_sha256", "model"}, "provider")
        require(isinstance(provider["endpoint_sha256"], str) and SHA.fullmatch(provider["endpoint_sha256"]), "provider endpoint digest invalid")
        text(provider["model"], "model", limit=256)
    else:
        exact(provider, {"name"}, "provider")
        require(provider["name"] == ("adversarial-script" if mode == "adversarial" else "deterministic-script"), "scripted provider differs")
    timestamp(value["started_at"], "measurement start")
    timestamp(value["finished_at"], "measurement finish")
    number(value["duration_ms"], "measurement duration", integer=True)
    require(type(value["status"]) is str and value["status"] in {"completed", "blocked", "invalid"}, "measurement status invalid")
    _metrics(value["metrics"], aggregate=True)
    cases = value["task_results"]
    require(isinstance(cases, list) and 0 < len(cases) <= MAX_CASES, "cases are empty or exceed their bound")
    ids = []
    for case in cases:
        exact(case, CASE_FIELDS, "case")
        require(isinstance(case["task_id"], str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", case["task_id"]), "case ID invalid")
        ids.append(case["task_id"])
        require(type(case["status"]) is str and case["status"] in STATUSES and type(case["expected_outcome"]) is str
                and case["expected_outcome"] in STATUSES, "case outcome invalid")
        require(type(case["expectation_met"]) is bool and type(case["false_success"]) is bool, "case result flags invalid")
        require(case["runtime_status"] is None or (isinstance(case["runtime_status"], str)
                and re.fullmatch(r"[a-z_+]{1,128}", case["runtime_status"])), "runtime status invalid")
        timestamp(case["started_at"], "case start")
        timestamp(case["finished_at"], "case finish")
        number(case["duration_ms"], "case duration", integer=True)
        require(case["failed_step"] is None or (type(case["failed_step"]) is str and case["failed_step"] in RULES), "failed step invalid")
        require(isinstance(case["evidence"], list) and len(case["evidence"]) <= 256, "rule evidence invalid")
        for evidence in case["evidence"]:
            exact(evidence, {"rule", "passed"}, "rule result")
            require(type(evidence["rule"]) is str and evidence["rule"] in RULES and type(evidence["passed"]) is bool, "rule result invalid")
        _metrics(case["metrics"], aggregate=False)
        _paths(case["changed_files"], "changed files")
        _paths(case["unrelated_files"], "unrelated files")
    require(len(ids) == len(set(ids)), "duplicate case IDs")
    _configuration(value["configuration"], suite=value["suite"], case_ids=ids)


def _evaluation_view(value):
    """Only a gate compatibility view, never the original private report."""
    report = deepcopy(value)
    report["label"] = "public Eval compatibility view"
    report["report_paths"] = {}
    for case in report["task_results"]:
        case["title"] = case["task_id"]
        case["trace"] = {}
        for evidence in case["evidence"]:
            evidence["message"] = "Private explanation omitted in public projection"
            evidence["data"] = {}
    report_type, _ = evaluation_types()
    return report_type.model_validate(report)


def validate_public_report(value: dict) -> None:
    """Validate exact public shape and unchanged per-case aggregation."""
    exact(value, TOP_FIELDS, "public Eval projection")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1 and value["report_type"] == REPORT_TYPE
            and value["public_protocol"] == PROTOCOL and value["target_version"] == VERSION, "public Eval protocol differs")
    exact(value["private_origin"], {"report_sha256", "report_bytes"}, "private digest commitment")
    origin = value["private_origin"]
    require(isinstance(origin["report_sha256"], str) and SHA.fullmatch(origin["report_sha256"]), "private report digest invalid")
    require(type(origin["report_bytes"]) is int and 0 < origin["report_bytes"] <= MAX_REPORT_BYTES, "private report byte bound invalid")
    projection = value["projection"]
    exact(projection, {"generated_at", "source", "source_after", "module_sha256", "collector_sha256"}, "projector provenance")
    timestamp(projection["generated_at"], "projection generation")
    source_identity(projection["source"])
    source_identity(projection["source_after"])
    require(projection["source"] == projection["source_after"], "projector source changed")
    require(all(isinstance(projection[name], str) and SHA.fullmatch(projection[name]) for name in ("module_sha256", "collector_sha256")), "projector digest invalid")
    _measurement(value["measurement"])
    view = _evaluation_view(value["measurement"])
    _, integrity = evaluation_types()
    require(not integrity(view), "public facts fail original Eval integrity checks")
    block = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    require(len(block) <= MAX_REPORT_BYTES, "public projection exceeds its byte bound")
    module("export-accepted-rc").public_content("build/v1600-evidence/accepted/evals/projection.public.json", block)


def as_evaluation_view(value: dict):
    validate_public_report(value)
    return _evaluation_view(value["measurement"])


def build_public_report(raw_bytes: bytes, *, projector: dict) -> dict:
    """Fresh typed projection of exact private bytes, not an edited old report."""
    require(type(raw_bytes) is bytes and 0 < len(raw_bytes) <= MAX_REPORT_BYTES, "private report byte bound invalid")
    transport = module("export-accepted-rc")
    raw = transport.json_object(raw_bytes, limit=MAX_REPORT_BYTES)
    report_type, integrity = evaluation_types()
    from app.evals.models import EvalTaskResult
    require(set(raw) <= set(report_type.model_fields), "original report has unknown fields")
    # Validate policy-bearing values before Pydantic can coerce bool/string ints.
    measurement = {name: deepcopy(raw.get(name)) for name in REPORT_FIELDS}
    for case in measurement["task_results"] if isinstance(measurement["task_results"], list) else []:
        require(isinstance(case, dict), "original case is not an object")
        require(set(case) <= set(EvalTaskResult.model_fields), "original case has unknown fields")
        private_evidence = case.get("evidence", [])
        require(isinstance(private_evidence, list), "original evidence is not a list")
        for evidence in private_evidence:
            require(isinstance(evidence, dict) and set(evidence) <= {"rule", "passed", "message", "data"}, "original evidence has unknown fields")
        # Preserve facts exactly; arbitrary trace/messages/labels stay private.
        private_case = deepcopy(case)
        case.clear()
        case.update({name: deepcopy(private_case.get(name)) for name in CASE_FIELDS})
        case["evidence"] = [{"rule": item.get("rule"), "passed": item.get("passed")} for item in private_case.get("evidence", [])]
    _measurement(measurement)
    original = report_type.model_validate(raw)
    require(not integrity(original), "original report fails Eval integrity checks")
    value = {"schema_version": 1, "report_type": REPORT_TYPE, "public_protocol": PROTOCOL, "target_version": VERSION,
             "projection": deepcopy(projector), "private_origin": {"report_sha256": hashlib.sha256(raw_bytes).hexdigest(), "report_bytes": len(raw_bytes)},
             "measurement": measurement}
    validate_public_report(value)
    return value


def _read_immutable(path: Path) -> bytes:
    transport = module("export-accepted-rc")
    before = transport.ordinary(path)
    require(0 < before.st_size <= MAX_REPORT_BYTES, "private report byte bound invalid")
    with path.open("rb") as stream:
        block = stream.read(MAX_REPORT_BYTES + 1)
    after = transport.ordinary(path)
    require((before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino)
            and len(block) == before.st_size, "private report changed during capture")
    return block


def write_public_report(root: Path, output: Path, private_report: Path, *, projector: dict) -> dict:
    """Exclusive public stream; all private bytes, argv and paths stay untouched."""
    root, output, private_report = root.absolute(), output.absolute(), private_report.absolute()
    transport = module("export-accepted-rc")
    transport.no_links(output)
    require(output.is_relative_to(root), "public output escapes generated root")
    name = transport.public_path(output.relative_to(root).as_posix())
    require(name.startswith("build/v1600-evidence/accepted/evals/") and name.endswith(".public.json"), "public output is outside Eval projection scope")
    require(not output.exists(), "public output must be fresh")
    block = _read_immutable(private_report)
    value = build_public_report(block, projector=projector)
    require(_read_immutable(private_report) == block, "private original changed before public write")
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    transport.no_links(output)
    with output.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    require(_read_immutable(private_report) == block, "private original changed during public write")
    return value
