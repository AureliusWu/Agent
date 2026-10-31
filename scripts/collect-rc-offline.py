"""Collect current-source offline RC checks, never desktop or release acceptance.

The default only prints the plan. --execute uses the existing controlled test
collector and four complete scripted Eval suites. Private traces stay local;
only independently validated, privacy-checked evidence is staged for download.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import rc_gate as gate
from rc_test_evidence import controlled_environment

ROOT = Path(__file__).resolve().parents[1]
SPECIAL = {"local_model_benchmark_basic", "startup_performance_comparison"}
OFFLINE_SUITES = tuple(name for name in gate.EVALUATIONS if name != "default_model")


def plan() -> dict:
    required = gate.required_gates()
    automated = [name for name in required["automated"] if name not in SPECIAL]
    return {
        "target_version": gate.VERSION,
        "automated": automated,
        "evaluations": list(OFFLINE_SUITES),
        "pending_requirements": [gate.requirement_id(category, name)
            for category, names in required.items() for name in names
            if category != "automated" or name in SPECIAL],
        "rc_eligible": False,
        "formal_release": False,
    }


def source_identity() -> dict:
    return gate.module("generate_build_info")._release_source_identity(ROOT)


def require_same_source(source: dict) -> None:
    if source_identity() != source:
        raise ValueError("source changed during collection; retain this run and recapture")


def reference(path: Path) -> dict:
    transport = gate.module("export-accepted-rc")
    if transport.ordinary(path).st_size > gate.MAX_REPORT_BYTES:
        raise ValueError("offline evidence exceeds the bounded reader")
    return {"path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def validate_checks(path: Path, source: dict, automated: list[str]) -> dict:
    proof = gate.read_json(path)
    gate.bound_source(proof, source)
    expected = {gate.requirement_id("automated", name): True for name in automated}
    if (proof.get("report_type") != "rc_check_evidence" or proof.get("target_version") != gate.VERSION
            or proof.get("kind") != "automated" or proof.get("actual_run") is not True
            or proof.get("status") != "PASS" or type(proof.get("exit_code")) is not int or proof["exit_code"] != 0
            or proof.get("timed_out") is not False or proof.get("checks") != expected
            or any(value is not True for value in proof["checks"].values())):
        raise ValueError("complete source-bound offline checks did not pass")
    for name in automated:
        gate.automated_command(proof, name, root=ROOT)
    gate.validate_backend_evidence(ROOT, proof, "python_full_tests", all_backend_checks=True)
    return proof


def validate_evaluation(path: Path, source: dict, name: str) -> None:
    from app.evals.contracts import task_contract
    from app.evals.loader import load_tasks

    public = gate.module("rc_eval_public")
    report = public.as_evaluation_view(gate.read_json(path))
    gate.bound_source(report.model_dump(mode="json"), source)
    tasks, suite, mode = gate.EVALUATIONS[name]
    expected = task_contract(load_tasks(ROOT / tasks, suite=suite), suite)
    metrics = report.metrics
    if (report.suite != suite or report.mode != mode or report.app_version != gate.VERSION
            or report.configuration.get("evaluation_contract") != expected
            or report.status != "completed" or metrics.get("task_count") != len(expected["task_ids"])
            or metrics.get("task_success_count") != metrics.get("task_count")
            or any(metrics.get(key) != 0 for key in (
                "false_success_count", "permission_violation_count", "sandbox_violation_count",
                "unrelated_file_modification_count"))):
        raise ValueError("the complete offline evaluation contract did not pass")


def stage_evidence(directory: Path, summary: dict, references: list[dict]) -> None:
    """Copy only a bounded explicit list; never recurse into raw test data."""
    transport = gate.module("export-accepted-rc")
    transport.no_links(directory)
    if directory.exists() or len(references) > 32:
        raise ValueError("offline download directory must be fresh and bounded")
    names = [item["path"] for item in references]
    if len(set(names)) != len(names):
        raise ValueError("duplicate evidence reference")
    validated = []
    for item in references:
        name = transport.public_path(item["path"])
        path = ROOT / name
        if reference(path) != item:
            raise ValueError("evidence changed before staging")
        # JSON and XML remain text even when a new filename is introduced.
        if path.suffix not in {".json", ".xml"}:
            raise ValueError("only explicit JSON/XML evidence may be downloaded")
        block = path.read_bytes()
        if hashlib.sha256(block).hexdigest() != item["sha256"]:
            raise ValueError("evidence changed while staging")
        transport.public_content(path.with_suffix(".json").name, block)
        validated.append((name, block))
    encoded = json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    transport.public_content("offline-summary.json", encoded)
    directory.mkdir(parents=True, exist_ok=False)
    for name, block in validated:
        output = directory / name
        output.parent.mkdir(parents=True, exist_ok=True)
        transport.no_links(output)
        with output.open("xb") as stream:
            stream.write(block)
    with (directory / "offline-summary.json").open("xb") as stream:
        stream.write(encoded)


def execute_collection() -> dict:
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        raise ValueError("offline RC collection requires Windows and CPython 3.12")
    expected_python = ROOT / "siyi/.venv/Scripts/python.exe"
    if Path(sys.executable).resolve() != expected_python.resolve():
        raise ValueError("use the repository locked backend virtual environment")
    source = source_identity()
    if source.get("workspace_clean") is not True or source.get("source_version") != gate.VERSION:
        raise ValueError("offline RC collection requires clean current source")
    identifier = uuid.uuid4().hex
    relative = Path("accepted/offline") / identifier
    directory = ROOT / "build/v1601-evidence" / relative
    transport = gate.module("export-accepted-rc")
    transport.no_links(directory)
    directory.mkdir(parents=True, exist_ok=False)
    private = directory / "private"
    private.mkdir()
    environment = controlled_environment(dict(os.environ))
    environment_file = private / "acceptance.env"
    environment_file.write_text("", encoding="utf-8")
    environment.update(AGENT_DATA_ROOT=str(private / "data"),
                       AGENT_DESKTOP_DATA_DIRECTORY=str(private / "data"),
                       AGENT_ENV_FILE=str(environment_file))
    description = plan()
    print(json.dumps({"phase": "offline_checks", "required": len(description["automated"])}), flush=True)
    command = [sys.executable, "scripts/record-rc-check.py"]
    for name in description["automated"]:
        command.extend(["--gate", name])
    command.extend(["--output", (relative / "checks.json").as_posix()])
    completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
    if completed.returncode != 0:
        raise ValueError("offline test collection failed; original outputs retained")
    require_same_source(source)
    proof = validate_checks(directory / "checks.json", source, description["automated"])
    files = [reference(directory / "checks.json"), *proof["test_results"].values()]
    evaluations = {}
    for name in OFFLINE_SUITES:
        print(json.dumps({"phase": "evaluation", "suite": name}), flush=True)
        tasks, suite, mode = gate.EVALUATIONS[name]
        output = private / name
        command = [sys.executable, "-m", "app.evals.cli", "run", "--label", "rc-offline-" + name,
                   "--mode", mode, "--suite", suite, "--tasks", str(ROOT / tasks),
                   "--capture-source", "--require-passed", "--output", str(output)]
        # Raw CLI output may name the private report; retain it only in this run.
        with (private / (name + ".log")).open("x", encoding="utf-8") as stream:
            completed = subprocess.run(command, cwd=ROOT / "siyi", env=environment,
                                       stdout=stream, stderr=subprocess.STDOUT, check=False)
        evaluation_exit_code = completed.returncode
        reports = list(output.glob("*/report.json"))
        if len(reports) != 1:
            raise ValueError("offline evaluation did not produce one original report")
        projected = ROOT / "build/v1601-evidence/accepted/evals" / (identifier + "-" + name + ".public.json")
        completed = subprocess.run([sys.executable, "scripts/record-rc-eval-public.py", "--report", str(reports[0]),
                                    "--output", projected.relative_to(ROOT).as_posix()],
                                   cwd=ROOT, env=environment, check=False)
        if completed.returncode != 0:
            raise ValueError("safe public evaluation projection failed")
        require_same_source(source)
        measurement = gate.read_json(projected)["measurement"]
        metrics = measurement["metrics"]
        print(json.dumps({"phase": "evaluation_result", "suite": name,
                          "task_count": metrics.get("task_count"),
                          "task_success_count": metrics.get("task_success_count"),
                          "failed_task_ids": [case["task_id"] for case in measurement["task_results"]
                                              if not case["expectation_met"]]}), flush=True)
        if evaluation_exit_code != 0:
            raise ValueError("offline evaluation failed; private original retained")
        validate_evaluation(projected, source, name)
        evaluations[name] = reference(projected)
        files.append(evaluations[name])
    summary = {
        "schema_version": 1, "report_type": "rc_offline_collection", "target_version": gate.VERSION,
        "status": "OFFLINE_CHECKS_COLLECTED_NOT_RC_ACCEPTED", "actual_run": True,
        "source": source, "source_after": source_identity(),
        "automated_checks": reference(directory / "checks.json"), "evaluations": evaluations,
        "collected_requirements": sorted(proof["checks"]),
        "pending_requirements": description["pending_requirements"],
        "pending_evidence": ["comparable_eval_baselines", "default_model_live_evaluation",
                             "runtime_file_qualification", "candidate_binaries_and_component_observations",
                             "complete_installed_lifecycle", "actual_desktop_and_manual_acceptance",
                             "release_metadata_snapshots", "immutable_rc_acceptance"],
        "files": files, "rc_eligible": False, "formal_release": False,
    }
    require_same_source(source)
    stage_evidence(ROOT / "build/v1601-evidence/offline-download" / identifier, summary, files)
    require_same_source(source)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run offline checks only; no install/model/microphone/publication")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"status": "PLANNED_NOT_EXECUTED", "actual_run": False, **plan()}, indent=2))
        return 0
    try:
        summary = execute_collection()
        print(json.dumps({"status": summary["status"], "collected_requirements": len(summary["collected_requirements"]),
                          "pending_requirements": len(summary["pending_requirements"]), "rc_eligible": False}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
        print(json.dumps({"status": "BLOCKED", "failure_type": type(exc).__name__,
                          "detail": "Offline collection failed; original evidence retained locally.", "rc_eligible": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
