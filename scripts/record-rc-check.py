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

from rc_gate import ROOT, VERSION, module, required_gates, requirement_id
from rc_test_evidence import controlled_environment

EXCLUDED = {"local_model_benchmark_basic", "startup_performance_comparison"}
FRONTEND = {
    "frontend_lint": "lint", "frontend_build": "build", "frontend_security_tests": "test:security",
    "frontend_desktop_tests": "test:desktop", "frontend_build_identity": "test:build-info", "voice_error_races": "test:voice-errors",
}


def command_for(gates: list[str]) -> tuple[list[str], str]:
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
    command, cwd = command_for(args.gate)
    executable = sys.executable if command[0] == "python" else shutil.which(command[0])
    if not executable:
        parser.error(f"required executable unavailable: {command[0]}")
    builder = module("generate_build_info")
    source = builder._release_source_identity(ROOT)
    started = datetime.now(timezone.utc).isoformat()
    environment = controlled_environment(dict(os.environ))
    backend_directory = output.with_suffix(".backend")
    full_suite = command[-1] == "scripts/test.ps1"
    if full_suite:
        if backend_directory.exists():
            parser.error("backend evidence directory must also be fresh")
        environment["SIYI_RC_BACKEND_EVIDENCE"] = str(backend_directory)
    # Inherit terminal output rather than duplicating possible application logs
    # or credentials into a release envelope. The existing scripts/test.ps1 owns
    # isolated test data and its cleanup. No shell string is evaluated here.
    completed = subprocess.run([executable, *command[1:]], cwd=ROOT / cwd, env=environment, check=False)
    after = builder._release_source_identity(ROOT)
    passed = completed.returncode == 0 and source == after
    proof = {"schema_version": 1, "report_type": "rc_check_evidence", "target_version": VERSION,
             "kind": "automated", "source": source, "source_after": after, "actual_run": True,
             "status": "PASS" if passed else "FAIL", "command": command, "cwd": cwd,
             "exit_code": completed.returncode, "timed_out": False, "started_at": started,
             "finished_at": datetime.now(timezone.utc).isoformat(),
             "checks": {requirement_id("automated", gate): passed for gate in set(args.gate)}}
    if full_suite:
        from rc_gate import validate_backend_evidence
        try:
            proof["test_results"] = {
                key: {"path": (backend_directory / filename).relative_to(ROOT).as_posix(),
                      "sha256": hashlib.sha256((backend_directory / filename).read_bytes()).hexdigest()}
                for key, filename in (("junit", "junit.xml"), ("coverage", "coverage.json"), ("collection", "collection.json"), ("execution", "execution.json"))
            }
            for gate in args.gate:
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
