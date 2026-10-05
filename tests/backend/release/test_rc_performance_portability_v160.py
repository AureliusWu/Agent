"""Portable actual argv, without rewriting any historical observation."""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "rc_performance_portability_tested", ROOT / "scripts/record-rc-performance.py"
)
assert spec and spec.loader
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def test_desktop_sample_uses_real_portable_argv_and_exact_interpreter(tmp_path, monkeypatch):
    """Stop before process creation: no EXE, microphone or model is launched."""
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    binary = {"path": "candidate/desktop.exe", "sha256": "a" * 64}
    monkeypatch.setattr(
        collector, "binary_reference",
        lambda path: {"path": path, "sha256": "b" * 64},
    )
    observed = {}

    class StopBeforeLaunch(Exception):
        pass

    def inspect_launch(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        raise StopBeforeLaunch

    monkeypatch.setattr(collector.subprocess, "run", inspect_launch)
    with pytest.raises(StopBeforeLaunch):
        collector.collect_desktop_series(
            binary, directory=tmp_path / "samples", label="candidate",
            cache_state="warm", startup_path="portable-desktop",
        )
    assert observed["command"][0] == Path(sys.executable).name
    assert observed["kwargs"]["executable"] == sys.executable
    assert observed["kwargs"]["cwd"] == tmp_path
    assert observed["command"][1] == "scripts/record-rc-desktop-startup.py"
    assert all(not Path(part).is_absolute() for part in observed["command"])
    assert observed["command"][-4:] == [
        "--cache-state", "warm", "--startup-path", "portable-desktop",
    ]


def test_python_cli_identity_is_actual_script_argv_not_rewritten_native_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    script_argv = ["scripts/record-rc-performance.py", "--measurement-object", "desktop"]
    native_argv = [sys._base_executable, *script_argv]
    monkeypatch.setattr(sys, "argv", script_argv)
    monkeypatch.setattr(sys, "orig_argv", native_argv)
    receipt = collector.python_cli_identity()
    assert receipt["command_protocol"] == "python-script-argv-v1"
    assert receipt["command"] == script_argv
    assert receipt["command"] is not script_argv
    assert receipt["process_argv_sha256"] == hashlib.sha256(
        json.dumps(native_argv, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert set(receipt["interpreter"]) == {
        "implementation", "version", "launcher_sha256", "runtime_sha256",
    }
    assert receipt["interpreter"]["implementation"] == "cpython"
    assert receipt["interpreter"]["version"] == list(sys.version_info[:3])
    assert receipt["interpreter"]["launcher_sha256"] == hashlib.sha256(
        Path(sys.executable).read_bytes()
    ).hexdigest()
    assert receipt["interpreter"]["runtime_sha256"] == hashlib.sha256(
        Path(sys._base_executable).read_bytes()
    ).hexdigest()
    assert str(tmp_path) not in json.dumps(receipt)
    assert str(Path(sys.executable).parent) not in json.dumps(receipt)


@pytest.mark.parametrize("mutation", ["runpy", "flags", "script", "runtime", "cwd"])
def test_python_cli_identity_rejects_indirect_or_misbound_execution(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    script_argv = ["scripts/record-rc-performance.py", "--measurement-object", "desktop"]
    native_argv = [sys._base_executable, *script_argv]
    if mutation == "runpy":
        native_argv = [sys._base_executable, "-c", "import runpy; runpy.run_path(...)" ]
    elif mutation == "flags":
        native_argv.insert(1, "-B")
    elif mutation == "script":
        script_argv[0] = "scripts/unrelated.py"
        native_argv = [sys._base_executable, *script_argv]
    elif mutation == "runtime":
        native_argv[0] = str(tmp_path / "unrelated-python.exe")
    elif mutation == "cwd":
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)
    monkeypatch.setattr(sys, "argv", script_argv)
    monkeypatch.setattr(sys, "orig_argv", native_argv)
    with pytest.raises(ValueError, match="direct|runtime|working"):
        collector.python_cli_identity()
