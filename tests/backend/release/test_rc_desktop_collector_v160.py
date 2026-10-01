from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("rc_desktop_collector_tested", ROOT / "scripts/record-rc-desktop-startup.py")
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def runtime_fixture(tmp_path, collector, monkeypatch):
    paths = {"desktop": tmp_path / "desktop.exe", "sidecar": tmp_path / "agent-backend.exe"}
    build = {"manifest_version": 1, "product_version": "16.0.0", "git_commit": "a" * 40,
             "source_fingerprint": "B" * 64, "build_id": "c" * 24, "database_schema_version": 46,
             "component_build_ids": {key: f"{key}-{'c' * 24}" for key in ("react", "tauri", "sidecar")},
             "workspace_state": "CLEAN", "build_type": "Release"}
    receipt = {"schema_version": 1, "report_type": "rc_desktop_runtime_observation", "protocol_version": "desktop-render-ready-v1",
               "status": "PASS", "actual_run": True, "acceptance_nonce": "our-nonce", "isolated_test_data": True,
               "desktop_render_ready": True, "sidecar_ready": True, "process_ids": {"desktop": 100, "sidecar": 200},
               "readiness_ms": 100, "components": {key: copy.deepcopy(build) for key in ("react", "tauri", "sidecar")}}
    processes = {100: {"ProcessId": 100, "ExecutablePath": str(paths["desktop"]), "ParentProcessId": 1},
                 200: {"ProcessId": 200, "ExecutablePath": str(paths["sidecar"]), "ParentProcessId": 100}}
    monkeypatch.setattr(collector, "_process_information", lambda pid: processes[pid])
    return paths, receipt, processes


@pytest.mark.parametrize("mutation", [None, "nonce", "pid", "binary", "parent", "react", "dirty", "no_render", "no_isolation"])
def test_collector_accepts_only_the_owned_render_and_actual_binary_process_pair(tmp_path, collector, monkeypatch, mutation):
    paths, receipt, processes = runtime_fixture(tmp_path, collector, monkeypatch)
    if mutation == "nonce": receipt["acceptance_nonce"] = "another-launch"
    elif mutation == "pid": receipt["process_ids"]["desktop"] = 999
    elif mutation == "binary": processes[200]["ExecutablePath"] = str(tmp_path / "old-sidecar.exe")
    elif mutation == "parent": processes[200]["ParentProcessId"] = 999
    elif mutation == "react": receipt["components"]["react"]["build_id"] = "d" * 24
    elif mutation == "dirty":
        for manifest in receipt["components"].values(): manifest["workspace_state"] = "DIRTY"
    elif mutation == "no_render": receipt["desktop_render_ready"] = False
    elif mutation == "no_isolation": receipt["isolated_test_data"] = False
    if mutation is None:
        assert collector.validate_receipt(receipt, nonce="our-nonce", pid=100, **paths)["workspace_state"] == "CLEAN"
    else:
        with pytest.raises(ValueError):
            collector.validate_receipt(receipt, nonce="our-nonce", pid=100, **paths)


def test_dirty_observation_requires_explicit_development_flag_and_stays_dirty(tmp_path, collector, monkeypatch):
    paths, receipt, _ = runtime_fixture(tmp_path, collector, monkeypatch)
    for manifest in receipt["components"].values(): manifest["workspace_state"] = "DIRTY"
    with pytest.raises(ValueError):
        collector.validate_receipt(receipt, nonce="our-nonce", pid=100, **paths)
    result = collector.validate_receipt(receipt, nonce="our-nonce", pid=100, allow_development=True, **paths)
    assert result["workspace_state"] == "DIRTY"
    source = Path(collector.__file__).read_text(encoding="utf-8")
    assert '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)' in source
    assert 'encoding="utf-8", check=False' in source
    assert 'with isolated_desktop_data(boundary, lambda: cleanup_owned_processes(' in source
    assert 'owner.value != process.pid or process.poll() is not None' in source
    assert 'close_owned_desktop(process, receipt.get("window_handle"))' in source
    assert 'EnumWindows' not in source
