from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import pytest


REPOSITORY = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("rc_raw_evidence_tested", REPOSITORY / "scripts/rc_test_evidence.py")
assert spec and spec.loader
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)

NATIVE_PROBES = (
    "test_revoke_during_staging_must_prevent_recovery_commit",
    "test_in_place_parent_reparse_at_commit_cannot_redirect_native_rename",
    "test_staging_tamper_after_hash_must_not_be_reported_restored",
    "test_parent_junction_swap_at_commit_must_not_write_outside_workspace",
)


def fixture_reports(root: Path):
    source = root / "siyi/app/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("first = 1\nsecond = 2\nthird = 3\nfourth = 4\nfifth = 5\n", encoding="utf-8")
    tests = root / "tests/backend/tools/test_file_recovery_review_v160.py"
    tests.parent.mkdir(parents=True)
    tests.write_text("\n".join(f"def {name}():\n    assert True\n" for name in NATIVE_PROBES), encoding="utf-8")
    suite = ET.Element("testsuite", tests="4", errors="0", failures="0", skipped="0")
    collected = []
    for name in NATIVE_PROBES:
        classname = "tests.backend.tools.test_file_recovery_review_v160"
        ET.SubElement(suite, "testcase", classname=classname, name=name)
        collected.append({"nodeid": f"tests/backend/tools/test_file_recovery_review_v160.py::{name}",
                          "classname": classname, "name": name})
    junit = root / "junit.xml"
    ET.ElementTree(suite).write(junit, encoding="utf-8")
    coverage = {"files": {"app/example.py": {"executed_lines": [1, 2, 3, 4], "missing_lines": [5],
                                             "summary": {"num_statements": 5, "covered_lines": 4}}},
                "totals": {"num_statements": 5, "covered_lines": 4}}
    return junit, coverage, {"schema_version": 1, "exit_code": 0, "collected": collected, "deselected": []}


def test_controlled_environment_discards_selection_plugins_and_coverage_overrides():
    environment = evidence.controlled_environment({"PATH": "native-tools", "PYTEST_ADDOPTS": "-k one --no-cov",
        "PyTest_Plugins": "skip_native", "COVERAGE_PROCESS_START": "empty.ini", "COV_CORE_SOURCE": "elsewhere",
        "PYTHONPATH": "foreign-plugin", "PYTHONOPTIMIZE": "2", "PYTHONUSERBASE": "foreign-packages"})
    assert environment["PATH"] == "native-tools"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert not (set(environment) & {"PYTEST_ADDOPTS", "PyTest_Plugins", "COVERAGE_PROCESS_START", "COV_CORE_SOURCE",
                                   "PYTHONPATH", "PYTHONOPTIMIZE", "PYTHONUSERBASE"})


def test_full_source_collection_and_real_coverage_at_eighty_pass(tmp_path):
    junit, coverage, collection = fixture_reports(tmp_path)
    result = evidence.validate_raw_results(tmp_path, junit, coverage, collection, "python_full_tests")
    assert result == {"case_count": 4, "mandatory_count": 0, "native_probe_count": 4, "coverage_percent": 80.0}


@pytest.mark.parametrize("mutation", ["native_skip", "renamed_native", "deselected", "missing_case", "failure",
                                       "forged_summary", "wrong_source", "low_coverage", "duplicate_source", "line_outside_source"])
def test_narrowed_skipped_or_fabricated_results_cannot_prove_a_full_gate(tmp_path, mutation):
    junit, coverage, collection = fixture_reports(tmp_path)
    tree = ET.parse(junit)
    suite = tree.getroot()
    cases = suite.findall("testcase")
    if mutation == "native_skip":
        ET.SubElement(cases[0], "skipped")
        suite.set("skipped", "1")
    elif mutation == "renamed_native":
        cases[0].set("name", "test_unrelated_success")
        collection["collected"][0]["name"] = "test_unrelated_success"
    elif mutation == "deselected":
        collection["deselected"] = [collection["collected"][0]["nodeid"]]
    elif mutation == "missing_case":
        suite.remove(cases[0])
        suite.set("tests", "3")
        collection["collected"].pop(0)
    elif mutation == "failure":
        ET.SubElement(cases[0], "failure")
    elif mutation == "forged_summary":
        coverage["files"]["app/example.py"]["summary"]["covered_lines"] = 5
    elif mutation == "wrong_source":
        coverage["files"]["../unrelated.py"] = coverage["files"].pop("app/example.py")
    elif mutation == "low_coverage":
        coverage["files"]["app/example.py"]["executed_lines"] = [1, 2, 3]
        coverage["files"]["app/example.py"]["missing_lines"] = [4, 5]
        coverage["files"]["app/example.py"]["summary"]["covered_lines"] = 3
        coverage["totals"]["covered_lines"] = 3
    elif mutation == "duplicate_source":
        coverage["files"][str(tmp_path / "siyi/app/example.py")] = copy.deepcopy(coverage["files"]["app/example.py"])
        coverage["totals"] = {"num_statements": 10, "covered_lines": 8}
    else:
        coverage["files"]["app/example.py"]["executed_lines"] = [101, 102, 103, 104]
    tree.write(junit, encoding="utf-8")
    with pytest.raises(ValueError):
        evidence.validate_raw_results(tmp_path, junit, coverage, collection, "python_full_tests")


def test_fixed_runner_validates_raw_results_even_after_zero_process_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    output = tmp_path / "build/v1600-evidence/isolated-run"
    monkeypatch.setattr(evidence.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 0})())
    assert evidence.run_backend(output) == 1, "exit zero without actual raw test attachments must fail"
    receipt = json.loads((output / "execution.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "FAIL"
    assert receipt["exit_code"] == 0


def test_fixed_pytest_command_cannot_inherit_host_selection(tmp_path):
    command = evidence.pytest_command(tmp_path, tmp_path / "results", "isolated-python")
    assert command[:3] == ["isolated-python", "-m", "pytest"]
    assert ["-o", "addopts="] == command[command.index("-o"):command.index("-o") + 2]
    assert "--cov-fail-under=80" in command
    assert f"--basetemp={tmp_path / 'results/pytest-temp'}" in command
    assert str(tmp_path / "tests/backend") in command
    assert "pytest_cov.plugin" in command and "anyio.pytest_plugin" in command
    assert not any(item.startswith(("-k", "-m=", "--ignore", "--no-cov")) for item in command[3:])


def test_actual_child_pytest_ignores_selection_and_plugin_environment(tmp_path, monkeypatch):
    """Only synthetic source/tests execute in the subprocess, never this app."""
    fixture_reports(tmp_path)
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(REPOSITORY / "scripts/rc_pytest_collection.py", tmp_path / "scripts/rc_pytest_collection.py")
    (tmp_path / "siyi/pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n[tool.coverage.run]\nsource = ["app"]\n', encoding="utf-8")
    test_file = tmp_path / "tests/backend/tools/test_file_recovery_review_v160.py"
    test_file.write_text("import app.example\n\n" + test_file.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    monkeypatch.setattr(evidence, "CRITICAL_FILES", {})
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k no_case_has_this_name --no-cov")
    monkeypatch.setenv("PYTEST_PLUGINS", "missing_injected_plugin")
    output = tmp_path / "build/v1600-evidence/isolated-child"
    assert evidence.run_backend(output) == 0
    receipt = json.loads((output / "execution.json").read_text(encoding="utf-8"))
    assert receipt["summary"]["case_count"] == 4
    assert receipt["summary"]["coverage_percent"] == 100.0
    assert receipt["status"] == "PASS"
