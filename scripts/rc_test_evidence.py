"""Controlled pytest execution and raw-result validation for offline RC checks."""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import platform
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CRITICAL_FILES = {
    "readonly_matrix": ("tools/test_permissions.py", "security/test_permission_broker_v940.py"),
    "recovery_matrix": ("runtime/test_recovery.py", "tools/test_file_recovery_v160.py"),
    "file_symlink_matrix": ("tools/test_sandbox.py",),
    "mcp_contract_tests": ("tools/test_mcp_contract_v150.py", "tools/test_mcp_discovery_v160.py"),
    "provider_contract_tests": ("providers/test_provider_contract_v160.py", "providers/test_effective_capabilities_v160.py"),
    "file_journal_crash_matrix": ("tools/test_file_journal_v160.py",),
    "file_undo_race_matrix": ("tools/test_file_recovery_v160.py", "tools/test_file_recovery_review_v160.py"),
    "file_resource_budget": ("tools/test_file_resource_boundaries_v160.py",),
    "database_migration_restore": ("tools/test_file_journal_migration_v160.py",),
}
NATIVE_PROBES = frozenset({
    "test_revoke_during_staging_must_prevent_recovery_commit",
    "test_in_place_parent_reparse_at_commit_cannot_redirect_native_rename",
    "test_staging_tamper_after_hash_must_not_be_reported_restored",
    "test_parent_junction_swap_at_commit_must_not_write_outside_workspace",
})
LEGACY_EXECUTION_PROTOCOL = "isolated-pytest-v1"
EXECUTION_PROTOCOL = "isolated-pytest-v2"


def controlled_environment(source: dict[str, str]) -> dict[str, str]:
    result = {key: value for key, value in source.items()
              if not key.upper().startswith(("PYTEST_", "COVERAGE_", "COV_CORE_", "AGENT_", "SIYI_"))
              and key.upper() not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONOPTIMIZE", "PYTHONUSERBASE", "PYTHONINSPECT"}}
    # Every required plugin is named by the fixed command, not host entry points.
    result.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", SIYI_ALLOW_PAID_API="false", SIYI_TEST_PROVIDER="mock")
    return result


def pytest_command(root: Path, directory: Path, python: str) -> list[str]:
    """Historical v1 argv, retained verbatim for validating old evidence only."""
    return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "pytest_cov.plugin",
            "-p", "anyio.pytest_plugin", "-p", "rc_pytest_collection", "-c", str(root / "siyi/pyproject.toml"),
            "-o", "addopts=", f"--rootdir={root}", str(root / "tests/backend"), f"--cov={root / 'siyi/app'}", "--cov-fail-under=80",
            f"--cov-config={root / 'siyi/pyproject.toml'}", "--cov-report=term-missing",
            f"--basetemp={directory / 'pytest-temp'}",
            f"--cov-report=json:{directory / 'coverage.json'}", f"--junitxml={directory / 'junit.xml'}",
            f"--rc-collection-output={directory / 'collection.json'}"]


def portable_pytest_command(root: Path, directory: Path, python: str) -> list[str]:
    """Actual v2 argv: all paths are relative to the fixed siyi working directory.

    The interpreter basename is the actual argv[0]. subprocess's explicit
    executable parameter binds it to the current interpreter without relying
    on PATH or publishing a personal installation path.
    """
    relative = directory.absolute().relative_to(root.absolute()).as_posix()
    if not relative.startswith("build/v1600-evidence/") or ".." in Path(relative).parts:
        raise ValueError("portable results must remain in generated v16 evidence")
    output = "../" + relative
    return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "pytest_cov.plugin",
            "-p", "anyio.pytest_plugin", "-p", "rc_pytest_collection", "-c", "pyproject.toml",
            "-o", "addopts=", "--rootdir=..", "../tests/backend", "--cov=app", "--cov-fail-under=80",
            "--cov-config=pyproject.toml", "--cov-report=term-missing",
            f"--basetemp={output}/pytest-temp", f"--cov-report=json:{output}/coverage.json",
            f"--junitxml={output}/junit.xml", f"--rc-collection-output={output}/collection.json"]


def validate_execution(root: Path, directory: Path, execution: dict[str, Any]) -> None:
    command = execution.get("command")
    if (execution.get("report_type") != "rc_backend_execution" or execution.get("actual_run") is not True
            or execution.get("status") != "PASS" or type(execution.get("exit_code")) is not int or execution.get("exit_code") != 0
            or execution.get("cwd") != "siyi" or not isinstance(command, list) or not command):
        raise ValueError("backend receipt is not the complete controlled pytest execution")
    protocol = execution.get("protocol_version")
    if protocol == LEGACY_EXECUTION_PROTOCOL:
        if type(execution.get("schema_version")) is not int or execution.get("schema_version") != 1 or command != pytest_command(root, directory, command[0]):
            raise ValueError("historical backend argv differs from the actual source/output paths")
        return
    interpreter = execution.get("interpreter")
    if (protocol != EXECUTION_PROTOCOL or execution.get("schema_version") != 2
            or command[0] not in {"python", "python3", "python.exe", "python3.exe"}
            or command != portable_pytest_command(root, directory, command[0])
            or not isinstance(interpreter, dict) or set(interpreter) != {"argv0", "implementation", "version", "sha256"}
            or interpreter.get("argv0") != command[0] or interpreter.get("implementation") != "CPython"
            or not isinstance(interpreter.get("version"), list) or len(interpreter["version"]) != 3
            or any(type(value) is not int for value in interpreter["version"])
            or interpreter["version"][:2] != [3, 12]
            or re.fullmatch(r"[0-9a-f]{64}", str(interpreter.get("sha256", ""))) is None
            or execution.get("pytest_configuration_sha256") != hashlib.sha256((root / "siyi/pyproject.toml").read_bytes()).hexdigest()
            or execution.get("environment_protocol") != "offline-isolated-data-v2"):
        raise ValueError("portable backend receipt does not match the fixed v2 execution protocol")


def validate_portable_coverage(coverage: dict[str, Any]) -> None:
    files = coverage.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("new v2 coverage must contain only relative application source paths")
    for name in files:
        normalized = name.replace("\\", "/") if isinstance(name, str) else ""
        if (not normalized.startswith("app/") or PurePosixPath(normalized).is_absolute()
                or PureWindowsPath(normalized).drive or ":" in normalized
                or any(part in {"", ".", ".."} for part in normalized.split("/"))):
            raise ValueError("new v2 coverage must contain only relative application source paths")


def run_backend(directory: Path) -> int:
    directory = directory.absolute()
    boundary = (ROOT / "build/v1600-evidence").resolve()
    for ancestor in (directory, *directory.parents):
        if ancestor.exists():
            metadata = ancestor.lstat()
            if ancestor.is_symlink() or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("backend evidence cannot use linked/reparse directories")
    if not directory.resolve().is_relative_to(boundary) or directory == boundary or directory.exists():
        raise ValueError("backend evidence must use a fresh directory under build/v1600-evidence")
    directory.mkdir(parents=True, exist_ok=False)
    environment = controlled_environment(dict(os.environ))
    isolated = directory / "isolated-data"
    isolated.mkdir()
    (isolated / "acceptance.env").write_text("", encoding="utf-8")
    environment.update(AGENT_DATA_ROOT=str(isolated), AGENT_DESKTOP_DATA_DIRECTORY=str(isolated),
                       AGENT_ENV_FILE=str(isolated / "acceptance.env"),
                       XDG_CACHE_HOME=str(isolated / "cache"), HF_HOME=str(isolated / "cache/huggingface"))
    environment["PYTHONPATH"] = str(ROOT / "scripts")
    environment["COVERAGE_FILE"] = str(directory / ".coverage")
    command = portable_pytest_command(ROOT, directory, Path(sys.executable).name)
    started = datetime.now(timezone.utc).isoformat()
    completed = subprocess.run(command, executable=sys.executable, cwd=ROOT / "siyi", env=environment, check=False)
    receipt: dict[str, Any] = {
        "schema_version": 2, "report_type": "rc_backend_execution", "protocol_version": EXECUTION_PROTOCOL,
        "actual_run": True, "command": command, "cwd": "siyi", "exit_code": completed.returncode,
        "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "FAIL", "raw_results": {},
        "environment_protocol": "offline-isolated-data-v2",
        "pytest_configuration_sha256": hashlib.sha256((ROOT / "siyi/pyproject.toml").read_bytes()).hexdigest(),
        "interpreter": {"argv0": command[0], "implementation": platform.python_implementation(),
                        "version": list(sys.version_info[:3]), "sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()},
    }
    try:
        if completed.returncode != 0:
            raise ValueError("the complete backend command failed")
        coverage = json.loads((directory / "coverage.json").read_text(encoding="utf-8"))
        collection = json.loads((directory / "collection.json").read_text(encoding="utf-8"))
        validate_portable_coverage(coverage)
        for gate in ("python_full_tests", *CRITICAL_FILES):
            receipt["summary"] = validate_raw_results(ROOT, directory / "junit.xml", coverage, collection, gate)
        receipt["raw_results"] = {
            key: {"path": (directory / filename).relative_to(ROOT).as_posix(),
                  "sha256": hashlib.sha256((directory / filename).read_bytes()).hexdigest()}
            for key, filename in (("junit", "junit.xml"), ("coverage", "coverage.json"), ("collection", "collection.json"))
        }
        receipt["status"] = "PASS"
        validate_execution(ROOT, directory, receipt)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        receipt["status"] = "FAIL"
        receipt["validation_error"] = str(exc)
    with (directory / "execution.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    return 0 if receipt["status"] == "PASS" else completed.returncode or 1


def validate_raw_results(root: Path, junit: Path, coverage: dict[str, Any], collection: dict[str, Any], gate: str) -> dict[str, Any]:
    if junit.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("JUnit exceeds bounded reader limit")
    xml = junit.read_bytes()
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("JUnit entities are not accepted")
    tree = ET.fromstring(xml)
    cases = tree.findall(".//testcase")
    keys = [(case.get("classname", ""), case.get("name", "")) for case in cases]
    if not cases or len(set(keys)) != len(keys) or any(not all(key) for key in keys):
        raise ValueError("JUnit cases empty, duplicated or unnamed")
    if any(case.find("failure") is not None or case.find("error") is not None for case in cases):
        raise ValueError("JUnit contains failed tests")
    suites = [tree] if tree.tag == "testsuite" else tree.findall(".//testsuite")
    if sum(int(suite.get("tests", "0")) for suite in suites) != len(cases):
        raise ValueError("JUnit summary does not equal the actual cases")
    if collection.get("schema_version") != 1 or collection.get("exit_code") != 0 or collection.get("deselected"):
        raise ValueError("pytest collection failed or tests were deselected")
    planned = collection.get("collected")
    if not isinstance(planned, list) or len(planned) != len(cases):
        raise ValueError("JUnit does not cover every collected case")
    expected = [(str(item.get("classname", "")), str(item.get("name", ""))) for item in planned]
    if len(set(expected)) != len(expected) or set(expected) != set(keys):
        raise ValueError("JUnit cases differ from the actual pre-selection collection")
    # A narrowed/forged collection cannot omit entire source test functions.
    test_files = sorted((root / "tests/backend").rglob("test_*.py"))
    for path in test_files:
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        parsed = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in parsed.body:
            functions = [node] if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else node.body if isinstance(node, ast.ClassDef) and node.name.startswith("Test") else []
            for function in functions:
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and function.name.startswith("test_"):
                    expected_class = module + ("." + node.name if isinstance(node, ast.ClassDef) else "")
                    if not any(cls.lstrip(".") == expected_class and name.partition("[")[0] == function.name for cls, name in keys):
                        raise ValueError(f"collected report omitted source test: {module}.{function.name}")
    modules = {"tests.backend." + item.removesuffix(".py").replace("/", ".") for item in CRITICAL_FILES.get(gate, ())}
    required = [case for case in cases if any(str(case.get("classname", "")).lstrip(".") == item or str(case.get("classname", "")).lstrip(".").startswith(item + ".") for item in modules)]
    if modules and (not required or any(case.find("skipped") is not None for case in required)):
        raise ValueError("mandatory specialized cases must execute and pass; skipped is not evidence")
    # All four Windows native commit probes are mandatory for every full RC run.
    native = [case for case in cases if str(case.get("classname", "")).lstrip(".") == "tests.backend.tools.test_file_recovery_review_v160"]
    if ({case.get("name") for case in native} != NATIVE_PROBES or len(native) != 4
            or any(case.find("skipped") is not None for case in native)):
        raise ValueError("all four native recovery probes must pass without skips")
    files = coverage.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("coverage has no measured source files")
    total = covered = 0
    measured: set[Path] = set()
    for filename, data in files.items():
        path = Path(filename.replace("\\", "/"))
        path = path.resolve() if path.is_absolute() else (root / "siyi" / path).resolve()
        if not path.is_relative_to((root / "siyi/app").resolve()):
            raise ValueError("coverage refers to a different source tree")
        if path in measured:
            raise ValueError("coverage measured the same source file twice")
        measured.add(path)
        executed, missing = data.get("executed_lines"), data.get("missing_lines")
        if not isinstance(executed, list) or not isinstance(missing, list) or any(type(line) is not int or line < 1 for line in executed + missing):
            raise ValueError("coverage line evidence is invalid")
        if len(set(executed + missing)) != len(executed) + len(missing):
            raise ValueError("coverage line evidence is duplicated or overlapping")
        line_count = len(path.read_text(encoding="utf-8-sig").splitlines())
        if any(line > line_count for line in executed + missing):
            raise ValueError("coverage lines do not exist in the current source file")
        summary = data.get("summary", {})
        if summary.get("num_statements") != len(executed) + len(missing) or summary.get("covered_lines") != len(executed):
            raise ValueError("coverage summary differs from raw line evidence")
        total += len(executed) + len(missing)
        covered += len(executed)
    expected_sources = {path.resolve() for path in (root / "siyi/app").rglob("*.py")}
    if not expected_sources or not expected_sources <= measured:
        raise ValueError("coverage omitted application source files")
    totals = coverage.get("totals", {})
    if total <= 0 or totals.get("num_statements") != total or totals.get("covered_lines") != covered:
        raise ValueError("coverage aggregate differs from file evidence")
    percentage = covered * 100 / total
    if not math.isfinite(percentage) or percentage < 80:
        raise ValueError("actual application coverage is below 80%")
    return {"case_count": len(cases), "mandatory_count": len(required), "native_probe_count": 4, "coverage_percent": percentage}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: rc_test_evidence.py <fresh-evidence-directory>")
    raise SystemExit(run_backend(Path(sys.argv[1])))
