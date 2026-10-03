"""Synthetic protocol tests; these do not qualify the real application's RC."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("portable_pytest_tested", ROOT / "scripts/rc_test_evidence.py")
assert spec and spec.loader
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


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
