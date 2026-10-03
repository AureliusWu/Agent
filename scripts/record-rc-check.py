"""Explicit, offline automated-check collector; never collect manual acceptance.

Uses the existing local test entry point. Output is created once, and records a
failure if source changed while tests ran. It cannot run arbitrary commands,
paid/live models, a microphone, installers or release publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from rc_gate import ROOT, VERSION, automated_command, module, read_json, required_gates, requirement_id
from rc_test_evidence import CRITICAL_FILES, controlled_environment, portable_pytest_command, run_backend

EXCLUDED = {"local_model_benchmark_basic", "startup_performance_comparison"}
BACKEND = {"python_full_tests", "coverage_80", *CRITICAL_FILES}
FRONTEND = {
    "frontend_lint": "lint", "frontend_build": "build", "frontend_security_tests": "test:security",
    "frontend_desktop_tests": "test:desktop", "frontend_build_identity": "test:build-info", "voice_error_races": "test:voice-errors",
}


def command_for(gates: list[str], *, backend_directory: Path | None = None) -> tuple[list[str], str]:
    if gates and set(gates) <= BACKEND:
        if backend_directory is None:
            raise ValueError("backend checks require their fresh evidence directory")
        return portable_pytest_command(ROOT, backend_directory, Path(sys.executable).name), "siyi"
    if len(gates) == 1 and gates[0] in FRONTEND:
        return ["npm", "run", FRONTEND[gates[0]]], "desktop/frontend"
    if gates == ["version_consistency"]:
        return ["python", "scripts/check-release-metadata.py"], "."
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/test.ps1"], "."


def main(argv: list[str] | None = None) -> int:
    choices = sorted(set(required_gates()["automated"]) - EXCLUDED)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", choices=choices, action="append", required=True)
    parser.add_argument("--output", required=True, help="New JSON under build/v1600-evidence")
    args = parser.parse_args(argv)
    root = (ROOT / "build/v1600-evidence").resolve()
    relative = Path(args.output)
    if relative.is_absolute() or ".." in relative.parts:
        parser.error("output must be relative to build/v1600-evidence")
    output = root / relative
    if not output.resolve().is_relative_to(root) or output.suffix != ".json" or output.exists():
        parser.error("output must be a fresh .json in the evidence root")
    for parent in (root, *output.parents):
        if parent == ROOT:
            break
        if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            parser.error("output parent cannot be a reparse point")
    backend_directory = output.with_suffix(".backend")
    backend_only = set(args.gate) <= BACKEND
    command, cwd = command_for(args.gate, backend_directory=backend_directory)
    full_suite = command[-1] == "scripts/test.ps1"
    if (backend_only or full_suite) and backend_directory.exists():
        parser.error("backend evidence directory must also be fresh")
    executable = None
    if not backend_only:
        executable = sys.executable if command[0] == "python" else shutil.which(command[0])
        if not executable:
            parser.error(f"required executable unavailable: {command[0]}")
    builder = module("generate_build_info")
    source = builder._release_source_identity(ROOT)
    started = datetime.now(timezone.utc).isoformat()
    environment = controlled_environment(dict(os.environ))
    if full_suite:
        environment["SIYI_RC_BACKEND_EVIDENCE"] = str(backend_directory)
    # Inherit terminal output rather than duplicating possible application logs
    # or credentials into a release envelope. The existing scripts/test.ps1 owns
    # isolated test data and its cleanup. No shell string is evaluated here.
    if backend_only:
        exit_code = run_backend(backend_directory)
    else:
        completed = subprocess.run([executable, *command[1:]], cwd=ROOT / cwd, env=environment, check=False)
        exit_code = completed.returncode
    after = builder._release_source_identity(ROOT)
    passed = exit_code == 0 and source == after
    proof = {"schema_version": 1, "report_type": "rc_check_evidence", "target_version": VERSION,
             "kind": "automated", "source": source, "source_after": after, "actual_run": True,
             "status": "PASS" if passed else "FAIL", "command": command, "cwd": cwd,
             "exit_code": exit_code, "timed_out": False, "started_at": started,
             "finished_at": datetime.now(timezone.utc).isoformat(),
             "checks": {requirement_id("automated", gate): passed for gate in set(args.gate)}}
    if backend_only or full_suite:
        from rc_gate import validate_backend_evidence
        try:
            proof["test_results"] = {
                key: {"path": (backend_directory / filename).relative_to(ROOT).as_posix(),
                      "sha256": hashlib.sha256((backend_directory / filename).read_bytes()).hexdigest()}
                for key, filename in (("junit", "junit.xml"), ("coverage", "coverage.json"), ("collection", "collection.json"), ("execution", "execution.json"))
            }
            if backend_only:
                execution = read_json(backend_directory / "execution.json")
                # The envelope describes pytest itself. The outer collector's
                # validation return code is a separately typed result, not argv.
                proof["exit_code"] = execution.get("exit_code")
                proof["collector_result"] = {"report_type": "rc_backend_collection_result", "exit_code": exit_code}
            for gate in args.gate:
                if backend_only:
                    automated_command(proof, gate, root=ROOT)
                else:
                    validate_backend_evidence(ROOT, proof, gate)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            passed = False
            proof["status"] = "FAIL"
            proof["validation_error"] = str(exc)
            proof["checks"] = {requirement_id("automated", gate): False for gate in set(args.gate)}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(proof, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": proof["status"], "scope": "development_check" if not source["workspace_clean"] else "source_check", "path": str(output)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
