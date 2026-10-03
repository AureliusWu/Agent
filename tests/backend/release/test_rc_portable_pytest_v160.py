"""Synthetic protocol tests; these do not qualify the real application's RC."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("portable_pytest_tested", ROOT / "scripts/rc_test_evidence.py")
assert spec and spec.loader
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
gate_spec = importlib.util.spec_from_file_location("portable_pytest_gate_tested", ROOT / "scripts/rc_gate.py")
assert gate_spec and gate_spec.loader
rc = importlib.util.module_from_spec(gate_spec)
gate_spec.loader.exec_module(rc)


def receipt(root: Path, directory: Path):
    configuration = root / "siyi/pyproject.toml"
    configuration.parent.mkdir(parents=True, exist_ok=True)
    configuration.write_text('[tool.coverage.run]\nsource = ["app"]\nrelative_files = true\n', encoding="utf-8")
    return {"schema_version": 2, "report_type": "rc_backend_execution",
            "protocol_version": evidence.EXECUTION_PROTOCOL, "actual_run": True,
            "status": "PASS", "exit_code": 0, "cwd": "siyi",
            "command": evidence.portable_pytest_command(root, directory, "python.exe"),
            "pytest_configuration_sha256": hashlib.sha256(configuration.read_bytes()).hexdigest(),
            "environment_protocol": "offline-isolated-data-v2",
            "interpreter": {"argv0": "python.exe", "implementation": "CPython",
                            "version": [3, 12, 9], "sha256": "a" * 64}}


def test_actual_fixed_relative_argv_revalidates_on_another_checkout(tmp_path):
    first, second = tmp_path / "first", tmp_path / "other-machine"
    suffix = "build/v1600-evidence/accepted/backend-run"
    original = receipt(first, first / suffix)
    receipt(second, second / suffix)
    assert original["command"] == evidence.portable_pytest_command(second, second / suffix, "python.exe")
    evidence.validate_execution(second, second / suffix, original)
    assert not any(str(first) in value or str(second) in value for value in original["command"])
    assert "../tests/backend" in original["command"]
    assert "--cov-fail-under=80" in original["command"]


def test_ci_parent_relative_output_is_canonical_without_changing_argv(tmp_path, monkeypatch):
    backend = tmp_path / "siyi"
    backend.mkdir()
    monkeypatch.chdir(backend)
    expected = tmp_path / "build/v1600-evidence/accepted/ci-backend"
    assert evidence.portable_pytest_command(tmp_path, Path("../build/v1600-evidence/accepted/ci-backend"), "python.exe") == (
        evidence.portable_pytest_command(tmp_path, expected, "python.exe"))


@pytest.mark.parametrize("mutation", ["version", "argv0", "pythonpath", "subset", "coverage", "config",
                                      "schema", "protocol", "environment", "exit", "bool_exit", "skip", "cwd", "extra_interpreter"])
def test_portability_does_not_relax_full_execution_contract(tmp_path, mutation):
    directory = tmp_path / "build/v1600-evidence/accepted/backend-run"
    value = receipt(tmp_path, directory)
    if mutation == "version":
        value["interpreter"]["version"] = [3, 11, 9]
    elif mutation == "argv0":
        value["command"][0] = str(tmp_path / "python.exe")
    elif mutation == "pythonpath":
        value["command"].append("-k one_test")
    elif mutation == "subset":
        value["command"][value["command"].index("../tests/backend")] = "../tests/backend/release"
    elif mutation == "coverage":
        value["command"][value["command"].index("--cov-fail-under=80")] = "--cov-fail-under=79"
    elif mutation == "config":
        value["pytest_configuration_sha256"] = "b" * 64
    elif mutation == "schema":
        value["schema_version"] = 1
    elif mutation == "protocol":
        value["protocol_version"] = "unknown"
    elif mutation == "environment":
        value["environment_protocol"] = "inherit-host-env"
    elif mutation == "exit":
        value["exit_code"] = 1
    elif mutation == "bool_exit":
        value["exit_code"] = False
    elif mutation == "skip":
        value["actual_run"] = False
    elif mutation == "cwd":
        value["cwd"] = "."
    else:
        value["interpreter"]["installation_path"] = "uncontrolled"
    with pytest.raises(ValueError):
        evidence.validate_execution(tmp_path, directory, value)


def test_old_absolute_receipt_remains_readable_only_on_its_original_checkout(tmp_path):
    directory = tmp_path / "build/v1600-evidence/old"
    value = receipt(tmp_path, directory)
    value.update(schema_version=1, protocol_version=evidence.LEGACY_EXECUTION_PROTOCOL,
                 command=evidence.pytest_command(tmp_path, directory, "old-python"))
    saved = copy.deepcopy(value)
    evidence.validate_execution(tmp_path, directory, value)
    assert value == saved
    other = tmp_path / "other"
    with pytest.raises(ValueError):
        evidence.validate_execution(other, other / "build/v1600-evidence/old", value)


@pytest.mark.parametrize("filename", ["../app/file.py", "/app/file.py", "siyi/app/file.py", "app/../secret.py",
                                      "app\\..\\secret.py", "app/./example.py", "app//example.py",
                                      "app/drive:stream.py", "//server/app/file.py"])
def test_new_coverage_cannot_embed_absolute_or_escaping_paths(filename):
    with pytest.raises(ValueError):
        evidence.validate_portable_coverage({"files": {filename: {}}})


def test_new_coverage_is_relative_and_controlled_environment_drops_user_config():
    evidence.validate_portable_coverage({"files": {"app/example.py": {}}})
    evidence.validate_portable_coverage({"files": {"app\\example.py": {}}})
    environment = evidence.controlled_environment({"PATH": "tools", "AGENT_ENV_FILE": "user.env",
        "AGENT_DATA_ROOT": "user-data", "AGENT_DESKTOP_DATA_DIRECTORY": "user-data",
        "SIYI_ALLOW_PAID_API": "true", "SIYI_TEST_PROVIDER": "cloud", "SIYI_OTHER_SECRET": "private"})
    assert environment == {"PATH": "tools", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                           "SIYI_ALLOW_PAID_API": "false", "SIYI_TEST_PROVIDER": "mock"}


def backend_reports(root: Path, directory: Path):
    """Complete synthetic source/attachment closure, never real RC evidence."""
    execution = receipt(root, directory)
    source = root / "siyi/app/example.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("first = 1\nsecond = 2\nthird = 3\nfourth = 4\nfifth = 5\n", encoding="utf-8")
    modules = {item for paths in evidence.CRITICAL_FILES.values() for item in paths}
    modules.add("tools/test_file_recovery_review_v160.py")
    suite = ET.Element("testsuite", errors="0", failures="0", skipped="0")
    planned = []
    for module in sorted(modules):
        names = sorted(evidence.NATIVE_PROBES) if module.endswith("test_file_recovery_review_v160.py") else ["test_required_case"]
        test_file = root / "tests/backend" / module
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("\n".join(f"def {name}():\n    assert True\n" for name in names), encoding="utf-8")
        classname = "tests.backend." + module.removesuffix(".py").replace("/", ".")
        for name in names:
            ET.SubElement(suite, "testcase", classname=classname, name=name)
            planned.append({"nodeid": f"tests/backend/{module}::{name}", "classname": classname, "name": name})
    suite.set("tests", str(len(planned)))
    directory.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(directory / "junit.xml", encoding="utf-8")
    coverage = {"files": {"app/example.py": {"executed_lines": [1, 2, 3, 4], "missing_lines": [5],
                                             "summary": {"num_statements": 5, "covered_lines": 4}}},
                "totals": {"num_statements": 5, "covered_lines": 4}}
    collection = {"schema_version": 1, "exit_code": 0, "collected": planned, "deselected": []}
    (directory / "coverage.json").write_text(json.dumps(coverage), encoding="utf-8")
    (directory / "collection.json").write_text(json.dumps(collection), encoding="utf-8")
    references = {key: file_reference(root, directory / filename) for key, filename in
                  (("junit", "junit.xml"), ("coverage", "coverage.json"), ("collection", "collection.json"))}
    execution["raw_results"] = copy.deepcopy(references)
    (directory / "execution.json").write_text(json.dumps(execution), encoding="utf-8")
    references["execution"] = file_reference(root, directory / "execution.json")
    return {"command": execution["command"], "cwd": "siyi", "test_results": references}, execution


def file_reference(root: Path, path: Path):
    return {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def rehash_reports(root: Path, raw: dict, execution: dict):
    directory = (root / raw["test_results"]["execution"]["path"]).parent
    for key in ("junit", "coverage", "collection"):
        raw["test_results"][key] = file_reference(root, root / raw["test_results"][key]["path"])
    execution["raw_results"] = {key: copy.deepcopy(raw["test_results"][key]) for key in ("junit", "coverage", "collection")}
    (directory / "execution.json").write_text(json.dumps(execution), encoding="utf-8")
    raw["test_results"]["execution"] = file_reference(root, directory / "execution.json")


@pytest.mark.parametrize("name", ["python_full_tests", "coverage_80", *evidence.CRITICAL_FILES])
def test_independent_v2_consumer_admits_only_complete_backend_reports(tmp_path, name):
    raw, _ = backend_reports(tmp_path, tmp_path / "build/v1600-evidence/backend-run")
    original = copy.deepcopy(raw)
    rc.automated_command(raw, name, root=tmp_path)
    assert raw == original


def test_independent_consumer_revalidates_same_bytes_at_another_checkout(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    raw, _ = backend_reports(first, first / "build/v1600-evidence/backend-run")
    shutil.copytree(first, second)
    rc.automated_command(raw, "python_full_tests", root=second)
    rc.automated_command(raw, "coverage_80", root=second)


@pytest.mark.parametrize("name", ["frontend_lint", "frontend_build", "frontend_security_tests", "version_consistency",
                                  "startup_performance_comparison", "local_model_benchmark_basic", "voice_basic"])
def test_independent_backend_command_cannot_claim_other_requirements(tmp_path, name):
    raw, _ = backend_reports(tmp_path, tmp_path / "build/v1600-evidence/backend-run")
    with pytest.raises(rc.GateError):
        rc.automated_command(raw, name, root=tmp_path)


@pytest.mark.parametrize("mutation", ["outer_selection", "inner_selection", "subset", "collector_argv", "fake_fullstack", "cwd",
                                      "schema", "protocol", "receipt_failure", "hash", "low_coverage",
                                      "absolute_coverage", "deselected", "native_skip", "symlink_skip", "specialized_skip"])
def test_independent_admission_preserves_every_existing_raw_gate(tmp_path, mutation):
    raw, execution = backend_reports(tmp_path, tmp_path / "build/v1600-evidence/backend-run")
    directory = (tmp_path / raw["test_results"]["execution"]["path"]).parent
    if mutation == "outer_selection":
        raw["command"] = [*raw["command"], "-k", "one_test"]
    elif mutation == "inner_selection":
        execution["command"] = [*execution["command"], "-k", "one_test"]
        raw["command"] = execution["command"]
    elif mutation == "subset":
        raw["command"][raw["command"].index("../tests/backend")] = "../tests/backend/release"
    elif mutation == "collector_argv":
        raw["command"] = ["python.exe", "../scripts/rc_test_evidence.py", "../build/v1600-evidence/backend-run"]
    elif mutation == "fake_fullstack":
        raw["command"] = ["powershell", "-NoProfile", "-Command", "Write-Output PASS", "scripts/test.ps1"]
        raw["cwd"] = "."
    elif mutation == "cwd":
        raw["cwd"] = "."
    elif mutation == "schema":
        execution["schema_version"] = 2.0
    elif mutation == "protocol":
        execution["protocol_version"] = evidence.LEGACY_EXECUTION_PROTOCOL
    elif mutation == "receipt_failure":
        execution["status"] = "FAIL"
    elif mutation == "hash":
        raw["test_results"]["execution"]["sha256"] = "f" * 64
    elif mutation in {"low_coverage", "absolute_coverage"}:
        coverage = json.loads((directory / "coverage.json").read_text(encoding="utf-8"))
        if mutation == "low_coverage":
            coverage["files"]["app/example.py"].update(executed_lines=[1, 2, 3], missing_lines=[4, 5],
                                                        summary={"num_statements": 5, "covered_lines": 3})
            coverage["totals"]["covered_lines"] = 3
        else:
            coverage["files"][str(tmp_path / "siyi/app/example.py")] = coverage["files"].pop("app/example.py")
        (directory / "coverage.json").write_text(json.dumps(coverage), encoding="utf-8")
    elif mutation == "deselected":
        collection = json.loads((directory / "collection.json").read_text(encoding="utf-8"))
        collection["deselected"] = [collection["collected"][0]["nodeid"]]
        (directory / "collection.json").write_text(json.dumps(collection), encoding="utf-8")
    else:
        tree = ET.parse(directory / "junit.xml")
        suffix = {"native_skip": "test_file_recovery_review_v160", "symlink_skip": "test_sandbox",
                  "specialized_skip": "test_provider_contract_v160"}[mutation]
        case = next(item for item in tree.findall(".//testcase") if item.get("classname", "").endswith(suffix))
        ET.SubElement(case, "skipped")
        tree.write(directory / "junit.xml", encoding="utf-8")
    if mutation != "hash":
        rehash_reports(tmp_path, raw, execution)
    with pytest.raises(rc.GateError):
        rc.automated_command(raw, "python_full_tests", root=tmp_path)


def test_matrix_uses_its_checkout_root_for_independent_backend_evidence(tmp_path):
    raw, _ = backend_reports(tmp_path, tmp_path / "build/v1600-evidence/backend-run")
    source = {"source_version": "16.0.0", "source_commit": "a" * 40,
              "source_tree_fingerprint": "b" * 64, "workspace_clean": True}
    identifier = rc.requirement_id("automated", "python_full_tests")
    raw.update(report_type="rc_check_evidence", target_version="16.0.0", kind="automated", actual_run=True,
               status="PASS", source=source, source_after=source, exit_code=0, timed_out=False, checks={identifier: True})
    proof = tmp_path / "build/v1600-evidence/proof.json"
    proof.write_text(json.dumps(raw), encoding="utf-8")
    matrix = {"target_version": "16.0.0", "protocol_version": rc.PROTOCOL,
              "release_gates": {"automated": [{"id": "python_full_tests", "requirement_id": identifier, "status": "PASS",
                  "evidence": [{"kind": "automated", "actual_run": True, "outcome": "PASS", "report": file_reference(tmp_path, proof)}]}]}}
    errors = rc.validate_matrix(tmp_path, matrix, source, "synthetic-build")
    assert not any(identifier + ":" in error for error in errors)


def load_collector(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    collector_spec = importlib.util.spec_from_file_location("portable_backend_collector_tested", ROOT / "scripts/record-rc-check.py")
    assert collector_spec and collector_spec.loader
    collector = importlib.util.module_from_spec(collector_spec)
    collector_spec.loader.exec_module(collector)
    return collector


def mock_backend_capture(root: Path, directory: Path, collector, *, exit_code: int = 0):
    raw, execution = backend_reports(root, directory)
    python = Path(collector.sys.executable).name
    execution["command"] = evidence.portable_pytest_command(root, directory, python)
    execution["interpreter"]["argv0"] = python
    execution["exit_code"] = exit_code
    execution["status"] = "PASS" if exit_code == 0 else "FAIL"
    raw["command"] = execution["command"]
    rehash_reports(root, raw, execution)


@pytest.mark.parametrize("mutation", ["none", "source_drift", "failed_pytest", "no_attachments"])
def test_backend_only_collector_runs_fresh_capture_and_preserves_actual_pytest(tmp_path, monkeypatch, mutation):
    from types import SimpleNamespace
    collector = load_collector(monkeypatch)
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    source = {"source_version": "16.0.0", "source_commit": "a" * 40,
              "source_tree_fingerprint": "b" * 64, "workspace_clean": True}
    after = {**source, "source_tree_fingerprint": "c" * 64} if mutation == "source_drift" else source
    identities = iter((source, after))
    monkeypatch.setattr(collector, "module", lambda name: SimpleNamespace(_release_source_identity=lambda root: next(identities)))
    captures = []

    def capture(directory):
        assert not directory.exists()
        captures.append(directory)
        if mutation != "no_attachments":
            mock_backend_capture(tmp_path, directory, collector, exit_code=7 if mutation == "failed_pytest" else 0)
        return 7 if mutation == "failed_pytest" else 0

    monkeypatch.setattr(collector, "run_backend", capture)
    monkeypatch.setattr(collector.subprocess, "run", lambda *args, **kwargs: pytest.fail("backend selection must not launch test.ps1"))
    result = collector.main(["--gate", "python_full_tests", "--gate", "coverage_80", "--output", "backend-proof.json"])
    assert result == (0 if mutation == "none" else 1)
    output = tmp_path / "build/v1600-evidence/backend-proof.json"
    proof = json.loads(output.read_text(encoding="utf-8"))
    directory = output.with_suffix(".backend")
    assert captures == [directory]
    assert proof["command"] == evidence.portable_pytest_command(tmp_path, directory, Path(collector.sys.executable).name)
    assert proof["cwd"] == "siyi"
    assert proof["source"] == source and proof["source_after"] == after
    assert proof["status"] == ("PASS" if mutation == "none" else "FAIL")
    assert set(proof["checks"].values()) == {mutation == "none"}
    if mutation != "no_attachments":
        assert proof["exit_code"] == (7 if mutation == "failed_pytest" else 0)
        assert proof["collector_result"]["report_type"] == "rc_backend_collection_result"
        execution = json.loads((directory / "execution.json").read_text(encoding="utf-8"))
        assert proof["command"] == execution["command"] and proof["cwd"] == execution["cwd"]


def test_backend_collector_refuses_existing_receipts_before_any_run(tmp_path, monkeypatch):
    collector = load_collector(monkeypatch)
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    existing = tmp_path / "build/v1600-evidence/backend-proof.backend"
    existing.mkdir(parents=True)
    sentinel = existing / "execution.json"
    sentinel.write_text('{"status":"historical FAIL"}', encoding="utf-8")
    before = sentinel.read_bytes()
    monkeypatch.setattr(collector, "run_backend", lambda directory: pytest.fail("existing receipt must not rerun"))
    with pytest.raises(SystemExit):
        collector.main(["--gate", "python_full_tests", "--output", "backend-proof.json"])
    assert sentinel.read_bytes() == before


def test_mixed_fullstack_selection_keeps_original_test_ps1_route(tmp_path, monkeypatch):
    from types import SimpleNamespace
    collector = load_collector(monkeypatch)
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    source = {"source_version": "16.0.0", "source_commit": "a" * 40,
              "source_tree_fingerprint": "b" * 64, "workspace_clean": True}
    monkeypatch.setattr(collector, "module", lambda name: SimpleNamespace(_release_source_identity=lambda root: source))
    monkeypatch.setattr(collector.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(collector, "run_backend", lambda directory: pytest.fail("mixed route belongs to test.ps1"))
    processes = []

    def run(command, **kwargs):
        processes.append((command, kwargs))
        directory = Path(kwargs["env"]["SIYI_RC_BACKEND_EVIDENCE"])
        mock_backend_capture(tmp_path, directory, collector)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(collector.subprocess, "run", run)
    assert collector.main(["--gate", "python_full_tests", "--gate", "frontend_lint", "--output", "fullstack.json"]) == 0
    proof = json.loads((tmp_path / "build/v1600-evidence/fullstack.json").read_text(encoding="utf-8"))
    assert proof["command"] == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/test.ps1"]
    assert proof["cwd"] == "." and "collector_result" not in proof
    assert processes[0][0][0] == "powershell.exe"
    assert processes[0][1]["cwd"] == tmp_path


def test_failed_backend_collection_does_not_mislabel_actual_zero_pytest_exit(tmp_path, monkeypatch):
    from types import SimpleNamespace
    collector = load_collector(monkeypatch)
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    source = {"source_version": "16.0.0", "source_commit": "a" * 40,
              "source_tree_fingerprint": "b" * 64, "workspace_clean": True}
    monkeypatch.setattr(collector, "module", lambda name: SimpleNamespace(_release_source_identity=lambda root: source))

    def capture(directory):
        mock_backend_capture(tmp_path, directory, collector)
        execution = json.loads((directory / "execution.json").read_text(encoding="utf-8"))
        execution["status"] = "FAIL"
        (directory / "execution.json").write_text(json.dumps(execution), encoding="utf-8")
        return 1

    monkeypatch.setattr(collector, "run_backend", capture)
    assert collector.main(["--gate", "coverage_80", "--output", "failed.json"]) == 1
    proof = json.loads((tmp_path / "build/v1600-evidence/failed.json").read_text(encoding="utf-8"))
    assert proof["status"] == "FAIL" and proof["exit_code"] == 0
    assert proof["collector_result"]["exit_code"] == 1
    assert set(proof["checks"].values()) == {False}
