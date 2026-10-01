import asyncio
import copy
import json

import pytest

from app.config import settings
from app.database import init_db
from app.providers.configuration import ProviderConfiguration, save_provider_configuration


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "siyi-runtime-qualification-test"
    root.mkdir()
    (root / ".runtime-qualification-owned").write_text("runtime-file-v1", encoding="utf-8")
    monkeypatch.setattr(settings, "database_path", root / "runtime.db")
    monkeypatch.setattr(settings, "log_path", root / "runtime.log")
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(root / "provider.json"))
    monkeypatch.setenv("AGENT_DATA_ROOT", str(root))
    save_provider_configuration(ProviderConfiguration(provider_id="mock", max_retries=0))
    init_db()
    return root


def test_scripted_runtime_uses_real_files_receipts_and_never_qualifies(isolated):
    from app.evals.local_model_benchmark.runtime_qualification import run_runtime_file_qualification, REQUIRED_CASES, validate_qualification_report
    report = asyncio.run(run_runtime_file_qualification(isolation_root=isolated, mode="scripted", samples=1))
    assert {row["case_id"] for row in report["case_results"]} == set(REQUIRED_CASES)
    assert all(row["status"] == "passed" for row in report["case_results"]), json.dumps([row for row in report["case_results"] if row["status"] != "passed"], ensure_ascii=False, indent=2)
    assert report["mode"] == "scripted"
    assert report["evidence_layer"] == "runtime_filesystem"
    assert not report["file_agent_qualified"]
    assert all(not item["qualified"] for item in report["qualification"].values())
    write = next(row for row in report["case_results"] if row["case_id"] == "file-create")
    assert write["evidence"]["executor"] == "LocalWindowsExecutor"
    assert write["evidence"]["filesystem_passed"]
    assert any(row["success"] and row["change_id"] for row in write["evidence"]["receipts"])
    assert report["metrics"]["failed_samples"] == 0
    assert validate_qualification_report(report, current_identity=report["effective_capabilities"])
    assert str(isolated) not in str(report)


def test_isolation_and_live_injection_fail_closed(isolated, monkeypatch, tmp_path):
    from app.evals.local_model_benchmark.runtime_qualification import run_runtime_file_qualification
    with pytest.raises(ValueError, match="isolated"):
        asyncio.run(run_runtime_file_qualification(isolation_root=tmp_path, mode="scripted", samples=1))
    with pytest.raises(ValueError, match="live"):
        asyncio.run(run_runtime_file_qualification(isolation_root=isolated, mode="local_live", samples=3, completion_factory=lambda *_: None))


def test_gate_rejects_missing_duplicated_failed_and_stale_evidence(isolated):
    from app.evals.local_model_benchmark.runtime_qualification import validate_qualification_report
    # Malformed and legacy envelopes must never crash the RC reader or qualify.
    for payload in (None, [], {}, {"benchmark_version": "local-model-v1", "release_gate_eligible": True}, {"case_results": [None]}):
        assert validate_qualification_report(payload, current_identity={})


def _synthetic_reader_fixture():
    """Parser-only fabricated fixture; never written as acceptance evidence."""
    import uuid
    from app.evals.local_model_benchmark.runtime_cases import CASES
    from app.evals.local_model_benchmark.runtime_qualification import _hash_files
    identity = {"provider_id": "ollama", "endpoint_hash": "a" * 16, "model": "reader-fixture", "model_digest": "b" * 64,
                "configuration_hash": "c" * 64, "identity_hash": "d" * 64, "local": True, "observation_stale": False, "context_window_tokens": 65536}
    payload = {"schema_version": 1, "target_version": "16.0.0", "protocol": "runtime-file-v1", "mode": "local_live",
               "evidence_layer": "runtime_filesystem", "filesystem": "NTFS", "effective_capabilities": identity,
               "identity_stable": True, "preflight_errors": [], "status": "passed", "case_results": []}
    for case in CASES:
        for sample in range(1, 4):
            task = uuid.uuid4().hex
            receipts = [{"receipt_id": uuid.uuid4().hex, "task_id": task, "tool": name,
                         "success": not case.control, "permission_decision": "evaluated" if case.control else "approved",
                         "operation_kind": "read" if name == "read_file" else "mutation", "change_id": "fixture-change",
                         "error_code": ("read_only_mode" if case.case_id == "kernel-readonly-deny" else "tool_error") if case.control else None}
                        for name, _ in case.actions]
            payload["case_results"].append({"case_id": case.case_id, "sample": sample,
                "requirement_id": f"V160-RUNTIME-FILE-V1-{case.case_id.upper()}", "origin": "scripted_control" if case.control else "local_live",
                "status": "passed", "error_type": None, "evidence": {
                    "task_id": task, "runtime": "app.runtime.runner.run_chat", "executor": "LocalWindowsExecutor",
                    "permission_mode": case.mode, "task_status": "completed", "filesystem_passed": True,
                    "outside_sentinel_unchanged": True, "response_passed": True, "receipts": receipts,
                    "observed_files_sha256": _hash_files(case.expected), "expected_files_sha256": _hash_files(case.expected),
                    "successful_model_requests": 1, "model_identities": ["ollama:reader-fixture"], "audit_count": 1,
                    "operation_statuses": ["completed"] if any(row["operation_kind"] == "mutation" for row in receipts) else [],
                }})
    return payload


@pytest.mark.parametrize("change", ["mode", "target", "protocol", "missing_case", "duplicate", "two_samples", "failed", "unknown", "receipt", "permission", "disk", "model", "identity", "stale", "task_reuse", "missing_audit", "operation_running", "malformed", "foreign_tool", "extra_unknown_permission", "control_extra_success"])
def test_rc_reader_rejects_tampered_layers_and_samples(change):
    from app.evals.local_model_benchmark.runtime_qualification import validate_qualification_report
    original = _synthetic_reader_fixture()
    assert validate_qualification_report(original, current_identity=original["effective_capabilities"]) == []
    payload = copy.deepcopy(original)
    write = next(row for row in payload["case_results"] if row["case_id"] == "file-create")
    if change == "mode": payload["mode"] = "scripted"
    elif change == "target": payload["target_version"] = "15.0.0"
    elif change == "protocol": payload["protocol"] = "local-model-v1"
    elif change == "missing_case": payload["case_results"] = [row for row in payload["case_results"] if row["case_id"] != "file-create"]
    elif change == "duplicate": payload["case_results"].append(copy.deepcopy(write))
    elif change == "two_samples": payload["case_results"].remove(write)
    elif change == "failed": write["status"] = "failed"
    elif change == "unknown": write["evidence"]["task_status"] = "unknown"
    elif change == "receipt": write["evidence"]["receipts"] = []
    elif change == "permission": write["evidence"]["receipts"][0]["permission_decision"] = "unknown"
    elif change == "disk": write["evidence"]["observed_files_sha256"] = {}
    elif change == "model": write["evidence"]["successful_model_requests"] = 0
    elif change == "identity": payload["effective_capabilities"]["configuration_hash"] = "e" * 64
    elif change == "stale": payload["effective_capabilities"]["observation_stale"] = True
    elif change == "task_reuse": payload["case_results"][1]["evidence"]["task_id"] = payload["case_results"][0]["evidence"]["task_id"]
    elif change == "missing_audit": write["evidence"]["audit_count"] = 0
    elif change == "operation_running": write["evidence"]["operation_statuses"] = ["running"]
    elif change == "malformed": write["sample"] = []
    elif change in {"foreign_tool", "extra_unknown_permission"}:
        import uuid
        extra = copy.deepcopy(write["evidence"]["receipts"][0])
        extra["receipt_id"] = uuid.uuid4().hex
        if change == "foreign_tool": extra["tool"] = "run_command"
        else: extra["permission_decision"] = "unknown"
        write["evidence"]["receipts"].append(extra)
    elif change == "control_extra_success":
        import uuid
        control = next(row for row in payload["case_results"] if row["case_id"] == "kernel-readonly-deny")
        extra = copy.deepcopy(control["evidence"]["receipts"][0])
        extra.update(receipt_id=uuid.uuid4().hex, tool="write_file", success=True, permission_decision="approved", error_code=None)
        control["evidence"]["receipts"].append(extra)
    assert validate_qualification_report(payload, current_identity=original["effective_capabilities"])


def test_unknown_live_identity_stops_before_any_runtime_dispatch(isolated, monkeypatch):
    import app.evals.local_model_benchmark.runtime_qualification as qualification
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "ollama")
    save_provider_configuration(ProviderConfiguration(provider_id="ollama", model="unobserved-fixture", base_url="http://127.0.0.1:11434", max_retries=0))
    async def unobserved_metadata(*args, **kwargs):
        return {"status": "ok"}
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.diagnostics", unobserved_metadata)
    monkeypatch.setattr(qualification, "_dispatch_runtime", lambda *args, **kwargs: pytest.fail("must not start Runtime/model"))
    report = asyncio.run(qualification.run_runtime_file_qualification(isolation_root=isolated, mode="local_live", samples=3))
    assert report["status"] == "blocked"
    assert report["case_results"] == []
    assert "unknown_window" in report["preflight_errors"]
    assert not report["file_agent_qualified"]


def test_model_success_narrative_cannot_replace_tool_receipts(isolated):
    from app.evals.local_model_benchmark.runtime_cases import scripted_completion
    from app.evals.local_model_benchmark.runtime_qualification import run_runtime_file_qualification
    def factory(case):
        if case.case_id != "file-create":
            return scripted_completion(case)
        async def dishonest(*args, **kwargs):
            return {"content": "The file was successfully created."}
        return dishonest
    report = asyncio.run(run_runtime_file_qualification(isolation_root=isolated, mode="scripted", samples=1, completion_factory=factory))
    row = next(row for row in report["case_results"] if row["case_id"] == "file-create")
    assert row["status"] == "failed"
    assert row["evidence"]["receipts"] == []
    assert row["evidence"]["filesystem_passed"] is False
    assert report["metrics"]["failed_samples"] >= 1
    assert not report["file_agent_qualified"]


def test_runtime_cli_leaves_user_database_and_configuration_untouched(tmp_path):
    import os
    import subprocess
    import sys
    user_db = tmp_path / "user.db"
    user_config = tmp_path / "user-config.json"
    user_db.write_bytes(b"user-database-sentinel")
    user_config.write_bytes(b"user-configuration-sentinel")
    output = tmp_path / "reports"
    from app.providers.effective_capabilities import context_profile_key
    profiles = {context_profile_key(ProviderConfiguration(provider_id="mock")): {"context_window_tokens": 65536, "supports_tools": True}}
    profiles_file = tmp_path / "explicit-context-profiles.json"
    original_profiles = json.dumps(profiles).encode("utf-8")
    profiles_file.write_bytes(original_profiles)
    environment = dict(os.environ, AGENT_DATABASE_PATH=str(user_db), AGENT_PROVIDER_CONFIG_PATH=str(user_config),
                       AGENT_MODEL_CONTEXT_PROFILES_JSON='{"do-not-inherit-user-default":{"api_key":"private-default-profile-marker"}}',
                       SIYI_TEST_PROVIDER="offline", SIYI_ALLOW_PAID_API="false")
    process = subprocess.run([sys.executable, "-B", "-m", "app.evals.local_model_benchmark.runtime_file_cli", "--mode", "scripted", "--samples", "1", "--context-profiles", str(profiles_file), "--output", str(output)], env=environment, capture_output=True, timeout=90)
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    assert user_db.read_bytes() == b"user-database-sentinel"
    assert user_config.read_bytes() == b"user-configuration-sentinel"
    assert profiles_file.read_bytes() == original_profiles
    report = json.loads((output / "RUNTIME_FILE_QUALIFICATION.json").read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert not report["file_agent_qualified"]
    assert "source" not in report
    assert str(tmp_path) not in json.dumps(report)
    assert report["effective_capabilities"]["explicit_profile"] == next(iter(profiles.values()))
    assert "private-default-profile-marker" not in json.dumps(report)


@pytest.mark.parametrize("profile", [
    {"context_window_tokens": True}, {"context_window_tokens": 0}, {"context_window_tokens": 10_000_001},
    {"context_window_tokens": 1024.0}, {"max_output_tokens": "32"}, {"supports_tools": 1},
    {"supports_streaming": "false"}, {"supports_json_schema": None}, {"api_key": "must-not-leak"},
    {"base_url": "https://user:secret@example.test"}, {"unrecognized": 1}, [],
])
def test_explicit_profile_copy_rejects_secrets_unknown_fields_and_wrong_types(tmp_path, profile):
    from app.evals.local_model_benchmark.runtime_file_cli import _read_context_profiles
    source = tmp_path / "profiles.json"
    source.write_text(json.dumps({"ollama:" + "a" * 16 + ":fixture": profile}), encoding="utf-8")
    before = source.read_bytes()
    with pytest.raises(ValueError) as error:
        _read_context_profiles(source)
    assert "must-not-leak" not in str(error.value) and str(tmp_path) not in str(error.value)
    assert source.read_bytes() == before


@pytest.mark.parametrize("payload", [
    "[]", '{"ollama:' + 'a' * 16 + ':fixture":{},"ollama:' + 'a' * 16 + ':fixture":{}}',
    '{"fixture":{"context_window_tokens":1024}}', '{"*":{"context_window_tokens":1024}}',
    '{"ollama:' + 'a' * 16 + ':bad..:fixture":{}}', " " * (128 * 1024 + 1),
], ids=["array", "duplicate_key", "unscoped", "wildcard", "unsafe_model", "oversized"])
def test_context_profile_copy_is_bounded_and_requires_scoped_unique_keys(tmp_path, payload):
    from app.evals.local_model_benchmark.runtime_file_cli import _read_context_profiles
    source = tmp_path / "profiles.json"
    source.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        _read_context_profiles(source)


def _mock_live_refresh(isolated, monkeypatch, *, drift=None):
    """Reader/refresh unit fixture only; no model or real acceptance evidence."""
    import app.providers.capabilities as capabilities
    import app.evals.local_model_benchmark.runtime_qualification as qualification
    from app.providers.effective_capabilities import context_profile_key, record_context_observation
    config = ProviderConfiguration(provider_id="ollama", base_url="http://127.0.0.1:11434", model="refresh-fixture", max_tokens=32, max_retries=0)
    save_provider_configuration(config)
    monkeypatch.setattr(settings, "model_context_profiles_json", "{}")
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "ollama")
    clock, calls, dispatches = [1000.0], [], []
    monkeypatch.setattr(capabilities.time, "time", lambda: clock[0])
    async def metadata(self):
        calls.append(self.config)
        record_context_observation(self.config, configured=65536,
            runtime=4096 if drift == "runtime" and len(calls) >= 3 else 65536,
            model_digest=("b" if drift == "digest" and len(calls) >= 3 else "a") * 64)
        return {"status": "ok"}
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.diagnostics", metadata)
    async def no_chat(*args, **kwargs):
        pytest.fail("mocked refresh tests may not call a model")
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.chat", no_chat)
    rows = _synthetic_reader_fixture()["case_results"]
    async def sample(case, number, *args, **kwargs):
        dispatches.append((case.case_id, number))
        row = copy.deepcopy(next(item for item in rows if item["case_id"] == case.case_id and item["sample"] == number))
        row["evidence"]["model_identities"] = ["ollama:refresh-fixture"]
        clock[0] += 301  # Every prior observation expires before next sample.
        if drift == "configuration":
            from dataclasses import replace
            save_provider_configuration(replace(config, max_tokens=64))
        elif drift == "explicit":
            settings.model_context_profiles_json = json.dumps({context_profile_key(config): {"context_window_tokens": 4096}})
        return row
    monkeypatch.setattr(qualification, "_run_sample", sample)
    return qualification, calls, dispatches


def test_live_metadata_renews_expired_ttl_without_model_calls_or_rebinding_identity(isolated, monkeypatch):
    qualification, calls, dispatches = _mock_live_refresh(isolated, monkeypatch)
    report = asyncio.run(qualification.run_runtime_file_qualification(isolation_root=isolated, mode="local_live", samples=3))
    assert len(dispatches) == 30
    assert len(calls) == 32  # Initial binding, each sample, final binding.
    assert report["identity_stable"] and report["preflight_errors"] == []
    assert report["file_agent_qualified"]  # Fabricated reader unit fixture only.
    assert all(config.model == "refresh-fixture" and config.max_tokens == 32 for config in calls)


@pytest.mark.parametrize("drift", ["digest", "runtime", "configuration", "explicit"])
def test_metadata_refresh_cannot_hide_identity_drift_or_dispatch_next_sample(isolated, monkeypatch, drift):
    qualification, calls, dispatches = _mock_live_refresh(isolated, monkeypatch, drift=drift)
    report = asyncio.run(qualification.run_runtime_file_qualification(isolation_root=isolated, mode="local_live", samples=3))
    assert len(dispatches) == 1
    assert len(calls) == (2 if drift == "configuration" else 3)
    assert report["status"] == "blocked"
    assert not report["identity_stable"]
    assert report["preflight_errors"]
    assert not any(item["qualified"] for item in report["qualification"].values())


def test_metadata_refresh_timeout_cancels_its_read_only_request(isolated, monkeypatch):
    from app.evals.local_model_benchmark.runtime_qualification import _refresh_live_identity
    save_provider_configuration(ProviderConfiguration(provider_id="ollama", base_url="http://127.0.0.1:11434", model="timeout-fixture"))
    cancelled = []
    async def metadata(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr("app.providers.ollama.OllamaProvider.diagnostics", metadata)
    _, errors = asyncio.run(_refresh_live_identity(expected=None, timeout=0.001))
    assert errors == ["metadata_refresh_failed"]
    assert cancelled == [True]
