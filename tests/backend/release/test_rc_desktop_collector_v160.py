from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

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
    assert 'encoding="utf-8", timeout=15, check=False' in source
    assert 'retained_data(ROOT, output' in source
    assert 'job = OwnedDesktopJob()' in source
    assert 'taskkill' not in source and 'TemporaryDirectory' not in source
    assert 'owner.value != process.pid or process.poll() is not None' in source
    assert 'close_owned_desktop(process, receipt.get("window_handle"))' in source
    assert 'EnumWindows' not in source


def test_desktop_environment_does_not_inherit_private_config_or_cache(collector, tmp_path):
    data = tmp_path / "owned-data"
    data.mkdir()
    result = collector.desktop_environment({"SYSTEMROOT": str(tmp_path / "synthetic-system-root"), "AGENT_ENV_FILE": "private.env",
        "AGENT_DATA_ROOT": "private-data", "AGENT_DEEPSEEK_API_KEY": "synthetic-secret-not-used",
        "SIYI_BUILD_MANIFEST": "old-build.json", "HTTP_PROXY": "http://private-proxy", "HF_HOME": "private-cache"}, data, "nonce")
    assert result["AGENT_DATA_ROOT"] == str(data)
    assert result["AGENT_ENV_FILE"] == str(data / "config/acceptance.env")
    assert (data / "config/acceptance.env").read_bytes() == b""
    assert result["AGENT_DEEPSEEK_API_KEY"] == ""
    assert result["SIYI_ALLOW_PAID_API"] == "false" and result["SIYI_TEST_PROVIDER"] == "mock"
    assert "HTTP_PROXY" not in result and "SIYI_BUILD_MANIFEST" not in result
    assert result["HF_HOME"].startswith(str(data))


def test_reused_environment_fixture_must_not_silently_read_or_overwrite_private_values(collector, tmp_path):
    data = tmp_path / "owned-data"
    (data / "config").mkdir(parents=True)
    fixture = data / "config/acceptance.env"
    fixture.write_bytes(b"SYNTHETIC_CHANGED_CONFIG=1")
    with pytest.raises(ValueError, match="ordinary empty"):
        collector.desktop_environment({}, data, "nonce")
    assert fixture.read_bytes() == b"SYNTHETIC_CHANGED_CONFIG=1"


@pytest.mark.skipif(os.name != "nt", reason="Windows collector entry point")
@pytest.mark.parametrize("failure", [None, "launch", "receipt", "window_close", "forced_cleanup", "accounting", "interrupt", "resumed_then_close_failure"])
def test_collector_main_retains_exact_cleanup_and_never_promotes_failed_launch_to_pass(tmp_path, collector, monkeypatch, failure):
    """Synthetic unit receipts are fixtures, not real desktop acceptance."""
    paths, receipt, processes = runtime_fixture(tmp_path, collector, monkeypatch)
    receipt["window_handle"] = 123
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    for path in paths.values():
        path.write_bytes(b"not an executable; mocked unit fixture")
    source = {"source_version": "16.0.0", "source_commit": "a" * 40,
              "workspace_clean": True, "source_tree_fingerprint": "B" * 64}
    def fake_module(name):
        if name == "generate_build_info":
            return SimpleNamespace(_release_source_identity=lambda _root: source.copy())
        assert name == "rc_payload_inventory"
        return SimpleNamespace(inventory=lambda _root, _reference: {"synthetic_inventory": True})
    monkeypatch.setattr(collector, "module", fake_module)
    monkeypatch.setattr(collector, "executable_attachment", lambda root, reference: root / reference["path"])
    cleanup = {"protocol_version": "exact-native-job-v1", "active_before_cleanup": 0,
               "active_after_cleanup": 0, "forced_termination": False, "job_handle_closed": True,
               "unassigned_cleanup_complete": True, "owned_process_handles_remaining": 0,
               "owned_thread_handles_remaining": 0, "errors": []}
    if failure == "forced_cleanup":
        cleanup.update(active_before_cleanup=1, forced_termination=True)
    elif failure == "accounting":
        cleanup.update(active_after_cleanup=None, errors=["synthetic native query denied"])
    class FakeJob:
        launch_events = []
        def launch(self, desktop, environment):
            if failure == "launch":
                raise OSError("synthetic assignment denied before ResumeThread")
            if failure == "interrupt":
                raise KeyboardInterrupt()
            if failure == "resumed_then_close_failure":
                self.launch_events = ["created_suspended", "resumed"]
                raise OSError("synthetic CloseThread failed after actual ResumeThread")
            observed = copy.deepcopy(receipt)
            observed["acceptance_nonce"] = environment["SIYI_DESKTOP_ACCEPTANCE_NONCE"]
            if failure == "receipt":
                observed["desktop_render_ready"] = False
            data = Path(environment["AGENT_DATA_ROOT"])
            (data / collector.RECEIPT_NAME).write_text(json.dumps(observed), encoding="utf-8")
            return SimpleNamespace(pid=100, poll=lambda: None, wait=lambda timeout: 0)
        def observe_sidecar(self, pid, expected):
            assert pid == 200 and expected == paths["sidecar"]
            return SimpleNamespace(wait=lambda timeout: 0)
        def wait_empty(self, timeout):
            return 0
        def cleanup(self):
            return cleanup.copy()
    monkeypatch.setattr(collector, "OwnedDesktopJob", FakeJob)
    def close(process, handle):
        if failure == "window_close":
            raise ValueError("synthetic HWND is not owned")
        assert process.pid == 100 and handle == 123
    monkeypatch.setattr(collector, "close_owned_desktop", close)
    output = tmp_path / "build/v1600-evidence/observation.json"
    args = ["--desktop", "desktop.exe", "--sidecar", "agent-backend.exe", "--output",
            "build/v1600-evidence/observation.json", "--cache-state", "warm", "--startup-path", "portable-desktop"]
    assert collector.main(args) == (0 if failure is None else 1)
    run = output.with_suffix(".run")
    assert (run / "data/config/acceptance.env").exists()
    completions = list((run / "launches").glob("*/completion.json"))
    assert len(completions) == 1
    completed = json.loads(completions[0].read_text(encoding="utf-8"))
    assert completed["process_cleanup"] == cleanup
    if failure is None:
        result = json.loads(output.read_text(encoding="utf-8"))
        assert result["status"] == completed["status"] == "PASS"
        assert result["process_cleanup"]["forced_termination"] is False
    else:
        assert not output.exists(), "failed or force-cleaned observation must never publish PASS"
        result = json.loads(output.with_suffix(".failure.json").read_text(encoding="utf-8"))
        assert result["status"] == completed["status"] == "FAIL"
        assert result["rc_eligible"] is False and result["process_cleanup"] == cleanup
        if failure == "resumed_then_close_failure":
            assert completed["actual_run"] is True and result["actual_run"] is True
