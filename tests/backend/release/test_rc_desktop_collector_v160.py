from __future__ import annotations

import copy
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
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
    build = {"manifest_version": 1, "product_version": "16.0.1", "git_commit": "a" * 40,
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


def test_startup_environment_uses_a_real_mock_provider_before_native_launch(collector, tmp_path, monkeypatch):
    data = tmp_path / "owned-data"
    data.mkdir()
    result = collector.desktop_environment({
        "AGENT_PROVIDER_CONFIG_PATH": "private-provider.json", "SIYI_MOCK_SCENARIO": "tool",
        "AGENT_NETWORK_ALLOWED_DOMAINS": "*", "AGENT_SPEECH_ENABLED": "true",
        "AGENT_TAVILY_API_KEY": "synthetic-key-never-used",
    }, data, "nonce")
    config = Path(result["AGENT_PROVIDER_CONFIG_PATH"])
    assert config == data / "config/startup-mock-provider.json"
    value = json.loads(config.read_text(encoding="utf-8"))
    assert value == {"provider_id": "mock", "base_url": "", "model": "", "timeout_seconds": 90,
                     "max_tokens": 8192, "max_retries": 0, "allow_tools": False, "allow_streaming": True}
    # Exercise the real configuration loader, not only an environment label.
    from app.providers.configuration import load_provider_configuration
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(config))
    actual = load_provider_configuration()
    assert actual.provider_id == "mock" and actual.max_retries == 0 and actual.allow_tools is False
    assert result["SIYI_MOCK_SCENARIO"] == "normal"
    assert result["AGENT_MODEL_MAX_RETRIES"] == "0"
    assert result["AGENT_SPEECH_ENABLED"] == result["AGENT_ALLOW_LOCAL_MCP"] == "false"
    assert result["AGENT_NETWORK_ALLOWED_DOMAINS"] == "localhost,127.0.0.1,::1"
    blocked = set(result["AGENT_NETWORK_BLOCKED_DOMAINS"].split(","))
    assert {"api.deepseek.com", "api.tavily.com", "api.search.brave.com"} <= blocked
    assert all(result[name] == "" for name in ("AGENT_DEEPSEEK_API_KEY", "AGENT_TAVILY_API_KEY",
                                               "AGENT_BRAVE_API_KEY", "AGENT_SPEECH_API_KEY"))
    # Unsupported setting names must not pretend to disable real DB switches.
    assert "AGENT_AUTO_TITLE_ENABLED" not in result and "AGENT_STT_ENABLED" not in result


@pytest.mark.parametrize("replacement", [b'{"provider_id":"deepseek"}', b'{}', b'not json'])
def test_startup_provider_fixture_never_overwrites_existing_untrusted_config(collector, tmp_path, replacement):
    data = tmp_path / "owned-data"
    config = data / "config/startup-mock-provider.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(replacement)
    with pytest.raises(ValueError, match="mock Provider"):
        collector.desktop_environment({}, data, "nonce")
    assert config.read_bytes() == replacement


def test_startup_provider_exact_safe_reuse_preserves_config_bytes(collector, tmp_path):
    data = tmp_path / "owned-data"
    data.mkdir()
    first = collector.desktop_environment({}, data, "first-nonce")
    path = Path(first["AGENT_PROVIDER_CONFIG_PATH"])
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
    second = collector.desktop_environment({}, data, "second-nonce")
    assert first["AGENT_PROVIDER_CONFIG_PATH"] == second["AGENT_PROVIDER_CONFIG_PATH"]
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == before


@pytest.mark.parametrize("mutation", ["duplicate", "boolean_retry", "integer_tools", "extra_property", "same_size_live_provider"])
def test_startup_provider_rejects_ambiguous_types_and_changed_existing_bytes(collector, tmp_path, mutation):
    data = tmp_path / "owned-data"
    data.mkdir()
    environment = collector.desktop_environment({}, data, "nonce")
    path = Path(environment["AGENT_PROVIDER_CONFIG_PATH"])
    value = dict(collector.MOCK_PROVIDER)
    if mutation == "boolean_retry":
        value["max_retries"] = False
    elif mutation == "integer_tools":
        value["allow_tools"] = 0
    elif mutation == "extra_property":
        value["unreviewed"] = False
    elif mutation == "same_size_live_provider":
        value["provider_id"] = "deepseek"
    changed = json.dumps(value)
    if mutation == "duplicate":
        changed = changed.replace('"provider_id": "mock"', '"provider_id": "deepseek", "provider_id": "mock"')
    original = path.read_bytes()
    info = path.stat()
    if mutation == "same_size_live_provider":
        # Valid live-provider JSON with exactly the previous size and mtime:
        # neither filesystem observation may substitute for content identity.
        changed += " " * (len(original) - len(changed.encode("utf-8")))
    path.write_bytes(changed.encode("utf-8"))
    if mutation == "same_size_live_provider":
        assert len(path.read_bytes()) == len(original)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="mock Provider"):
        collector.desktop_environment({}, data, "another-nonce")
    assert path.read_bytes() == before and path.read_bytes() != original


def startup_database_fixture(data):
    path = data / "data/agent.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE stt_settings(singleton INTEGER, enabled INTEGER);
          INSERT INTO stt_settings VALUES(1,0);
          CREATE TABLE tts_settings(singleton INTEGER, enabled INTEGER, playback_mode TEXT);
          INSERT INTO tts_settings VALUES(1,0,'MANUAL');
          CREATE TABLE microphone_settings(singleton INTEGER, auto_send INTEGER);
          INSERT INTO microphone_settings VALUES(1,0);
          CREATE TABLE conversations(id INTEGER);
          CREATE TABLE messages(id INTEGER);
          CREATE TABLE agent_tasks(id INTEGER);
          CREATE TABLE conversation_title_jobs(id INTEGER);
          CREATE TABLE stt_requests(id INTEGER);
          CREATE TABLE tts_requests(id INTEGER);
          CREATE TABLE audio_playback_records(id INTEGER);
        """)
    return path


@pytest.mark.parametrize("change", [None, "stt", "tts", "playback", "mic", "conversation", "message", "task", "title", "stt_request", "tts_request", "playback_request"])
def test_startup_fixture_is_read_only_and_rejects_tasks_titles_and_speech(collector, tmp_path, change):
    data = tmp_path / "owned-data"
    collector.desktop_environment({}, data, "nonce")
    path = startup_database_fixture(data)
    mutations = {
        "stt": "UPDATE stt_settings SET enabled=1", "tts": "UPDATE tts_settings SET enabled=1",
        "playback": "UPDATE tts_settings SET playback_mode='AUTO'", "mic": "UPDATE microphone_settings SET auto_send=1",
        "conversation": "INSERT INTO conversations VALUES(1)", "message": "INSERT INTO messages VALUES(1)",
        "task": "INSERT INTO agent_tasks VALUES(1)", "title": "INSERT INTO conversation_title_jobs VALUES(1)",
        "stt_request": "INSERT INTO stt_requests VALUES(1)", "tts_request": "INSERT INTO tts_requests VALUES(1)",
        "playback_request": "INSERT INTO audio_playback_records VALUES(1)",
    }
    if change:
        with sqlite3.connect(path) as db:
            db.execute(mutations[change])
    before = path.read_bytes()
    if change is None:
        summary = collector.validate_startup_fixture(data)
        assert summary["provider"] == "mock" and summary["max_retries"] == 0
        assert summary["title_jobs"] == summary["tasks"] == summary["stt_enabled"] == summary["tts_enabled"] == 0
        assert summary["credential_namespace"] == "NOT_ISOLATED_SAME_WINDOWS_USER"
        assert summary["credential_values_recorded"] is False
    else:
        with pytest.raises(ValueError, match="startup fixture"):
            collector.validate_startup_fixture(data)
    assert path.read_bytes() == before


def test_startup_fixture_missing_database_is_not_created_or_qualified(collector, tmp_path):
    data = tmp_path / "owned-data"
    collector.desktop_environment({}, data, "nonce")
    with pytest.raises(ValueError, match="startup fixture"):
        collector.validate_startup_fixture(data)
    assert not (data / "data/agent.db").exists()


def test_real_production_database_defaults_keep_fresh_startup_speech_disabled(collector, tmp_path, monkeypatch):
    from app.config import settings
    from app.database import init_db
    data = tmp_path / "owned-data"
    data.mkdir()
    collector.desktop_environment({}, data, "nonce")
    monkeypatch.setattr(settings, "database_path", data / "data/agent.db")
    init_db()
    summary = collector.validate_startup_fixture(data)
    assert summary["stt_enabled"] == summary["tts_enabled"] == summary["microphone_auto_send"] == 0
    assert summary["title_jobs"] == summary["tasks"] == summary["conversations"] == 0


@pytest.mark.parametrize("domain", ["api.deepseek.com", "api.tavily.com", "api.search.brave.com"])
def test_startup_network_policy_blocks_real_model_and_search_before_dns(collector, tmp_path, monkeypatch, domain):
    from app.config import settings
    from app.security import network_security
    data = tmp_path / "owned-data"
    data.mkdir()
    environment = collector.desktop_environment({}, data, "nonce")
    monkeypatch.setattr(settings, "network_allowed_domains", environment["AGENT_NETWORK_ALLOWED_DOMAINS"])
    monkeypatch.setattr(settings, "network_blocked_domains", environment["AGENT_NETWORK_BLOCKED_DOMAINS"])

    async def forbidden_dns(*args):
        pytest.fail("blocked production endpoint must not reach DNS or a client")
    monkeypatch.setattr(network_security, "_resolve_host", forbidden_dns)
    # Search providers have their own allowed_domains; global blocked rules
    # must still win rather than relying only on the generic allowlist.
    with pytest.raises(network_security.NetworkPolicyError, match="Blocked outbound domain"):
        asyncio.run(network_security.validate_outbound_url(
            f"https://{domain}/", purpose="startup_unit_test", allowed_domains=(domain,),
        ))


@pytest.mark.skipif(os.name != "nt", reason="Windows collector entry point")
@pytest.mark.parametrize("failure", [None, "launch", "receipt", "window_close", "forced_cleanup", "accounting", "interrupt", "resumed_then_close_failure",
                                    "startup_workload", "startup_provider_drift", "shutdown_workload", "shutdown_provider_drift", "prelaunch_workload"])
def test_collector_main_retains_exact_cleanup_and_never_promotes_failed_launch_to_pass(tmp_path, collector, monkeypatch, failure):
    """Synthetic unit receipts are fixtures, not real desktop acceptance."""
    paths, receipt, processes = runtime_fixture(tmp_path, collector, monkeypatch)
    receipt["window_handle"] = 123
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    for path in paths.values():
        path.write_bytes(b"not an executable; mocked unit fixture")
    source = {"source_version": "16.0.1", "source_commit": "a" * 40,
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
    if failure == "prelaunch_workload":
        original_environment = collector.desktop_environment
        def environment_with_prior_title(source, data, nonce):
            environment = original_environment(source, data, nonce)
            fixture = startup_database_fixture(data)
            with sqlite3.connect(fixture) as db:
                db.execute("INSERT INTO conversation_title_jobs VALUES(1)")
            return environment
        monkeypatch.setattr(collector, "desktop_environment", environment_with_prior_title)
        cleanup = {"protocol_version": "exact-native-job-v1", "active_before_cleanup": None,
                   "active_after_cleanup": None, "forced_termination": None, "job_handle_closed": None,
                   "not_created": True, "errors": []}
    launch_calls = []
    class FakeJob:
        launch_events = []
        def launch(self, desktop, environment):
            launch_calls.append(True)
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
            fixture = startup_database_fixture(data)
            if failure == "startup_workload":
                with sqlite3.connect(fixture) as db:
                    db.execute("INSERT INTO conversation_title_jobs VALUES(1)")
            elif failure == "startup_provider_drift":
                Path(environment["AGENT_PROVIDER_CONFIG_PATH"]).write_text('{"provider_id":"deepseek"}', encoding="utf-8")
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
        run_data = tmp_path / "build/v1601-evidence/observation.run/data"
        if failure == "shutdown_workload":
            with sqlite3.connect(run_data / "data/agent.db") as db:
                db.execute("INSERT INTO agent_tasks VALUES(1)")
        elif failure == "shutdown_provider_drift":
            (run_data / "config/startup-mock-provider.json").write_text('{"provider_id":"deepseek"}', encoding="utf-8")
    monkeypatch.setattr(collector, "close_owned_desktop", close)
    output = tmp_path / "build/v1601-evidence/observation.json"
    args = ["--desktop", "desktop.exe", "--sidecar", "agent-backend.exe", "--output",
            "build/v1601-evidence/observation.json", "--cache-state", "warm", "--startup-path", "portable-desktop"]
    assert collector.main(args) == (0 if failure is None else 1)
    if failure == "prelaunch_workload":
        assert launch_calls == [], "unsafe retained workload must be rejected before any native launch"
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
