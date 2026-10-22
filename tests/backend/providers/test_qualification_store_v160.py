import json
import uuid
import asyncio
from dataclasses import replace

import pytest

from app.config import settings
from app.providers.configuration import ProviderConfiguration, save_provider_configuration
from app.providers.effective_capabilities import record_context_observation, resolve_effective_capabilities


@pytest.fixture
def selected(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))
    monkeypatch.setattr(settings, "model_context_profiles_json", "{}")
    config = ProviderConfiguration(provider_id="ollama", base_url="http://127.0.0.1:11434", model="store-fixture", max_retries=0)
    save_provider_configuration(config)
    import app.local_runtime.deployment_identity as deployment
    monkeypatch.setattr(deployment, "service_instance", lambda _: {"pid": 123, "creation": "win:123"})
    record_context_observation(config, configured=65536, runtime=65536, model_digest="a" * 64, deployment=_deployment())
    return config


def _deployment():
    from app.local_runtime.deployment_identity import observe_deployment
    # Fabricated OS/metadata fixture only. Never qualifies a real service.
    return observe_deployment("http://127.0.0.1:11434", "fixture-1", {
        "digest": "a" * 64, "context_length": 65536, "size": 4096, "size_vram": 4096,
    }, {"pid": 123, "creation": "win:123"})


def test_public_profile_has_unknown_four_tiers_without_report(selected):
    snapshot = resolve_effective_capabilities().public()
    assert set(snapshot["qualification"]["levels"]) == {"basic", "readonly_tools", "structured_plan", "file_agent"}
    assert all(level["status"] == "NOT_RUN" and not level["qualified"] for level in snapshot["qualification"]["levels"].values())
    assert snapshot["file_agent_qualified"] is False


@pytest.mark.parametrize("value", ['{"protocol":"local-model-v1"}', '{"mode":"scripted"}', '{"a":1,"a":2}', '[]', '"' + 'x' * (512 * 1024) + '"', '"\ud800"'], ids=["legacy", "scripted", "duplicate_keys", "array", "oversized", "invalid_unicode"])
def test_import_rejects_legacy_scripted_malformed_or_oversized_without_writing(selected, tmp_path, value):
    from app.providers.qualification_store import QualificationImportError, import_qualification_report
    with pytest.raises(QualificationImportError):
        import_qualification_report(value)
    assert not (tmp_path / "model-qualifications").exists()


def test_corrupt_summary_is_invalid_not_a_server_error(selected, tmp_path):
    from app.providers.qualification_store import qualification_status
    snapshot = resolve_effective_capabilities().public()
    directory = tmp_path / "model-qualifications"
    directory.mkdir()
    (directory / (snapshot["configuration_hash"] + ".json")).write_text("[]", encoding="utf-8")
    status = qualification_status(snapshot)
    assert status["status"] == "INVALID"
    assert not status["levels"]["file_agent"]["qualified"]


def _reader_fixture(snapshot):
    """Fabricated import fixture; not a live model report or release artifact."""
    from app.evals.local_model_benchmark.runtime_cases import CASES
    from app.evals.local_model_benchmark.runtime_qualification import _hash_files
    report = {"schema_version": 1, "target_version": "16.0.0", "protocol": "runtime-file-v1", "mode": "local_live",
              "run_id": uuid.uuid4().hex, "finished_at": "2026-09-30T00:00:00+00:00", "evidence_layer": "runtime_filesystem",
              "filesystem": "NTFS", "identity_stable": True, "effective_capabilities": snapshot,
              "preflight_errors": [], "status": "passed", "case_results": []}
    for case in CASES:
        for sample in range(1, 4):
            task = uuid.uuid4().hex
            receipts = [{"receipt_id": uuid.uuid4().hex, "task_id": task, "tool": name, "success": not case.control,
                         "permission_decision": "evaluated" if case.control else "approved", "operation_kind": "read" if name == "read_file" else "mutation",
                         "change_id": "fixture-change", "error_code": ("read_only_mode" if case.case_id == "kernel-readonly-deny" else "tool_error") if case.control else None}
                        for name, _ in case.actions]
            report["case_results"].append({"case_id": case.case_id, "sample": sample, "requirement_id": f"V160-RUNTIME-FILE-V1-{case.case_id.upper()}",
                "origin": "scripted_control" if case.control else "local_live", "status": "passed", "error_type": None, "evidence": {
                    "task_id": task, "runtime": "app.runtime.runner.run_chat", "executor": "LocalWindowsExecutor", "permission_mode": case.mode,
                    "task_status": "completed", "filesystem_passed": True, "outside_sentinel_unchanged": True, "response_passed": True,
                    "receipts": receipts, "observed_files_sha256": _hash_files(case.expected), "expected_files_sha256": _hash_files(case.expected),
                    "successful_model_requests": 1, "model_identities": [f"ollama:{snapshot['model']}"], "audit_count": 1,
                    "operation_statuses": ["completed"] if any(item["operation_kind"] == "mutation" for item in receipts) else [],
                }})
    return report


def test_legacy_report_without_deployment_cannot_grant_qualification(selected):
    from app.providers.qualification_store import QualificationImportError, import_qualification_report
    report = _reader_fixture(resolve_effective_capabilities().public())
    report["effective_capabilities"].pop("deployment_hash", None)
    with pytest.raises(QualificationImportError, match="ineligible"):
        import_qualification_report(json.dumps(report))


def _diagnostic_fixture(selected, monkeypatch):
    from app.providers.ollama import OllamaProvider
    from app.providers.qualification_store import import_qualification_report
    import_qualification_report(json.dumps(_reader_fixture(resolve_effective_capabilities().public())))
    assert resolve_effective_capabilities().file_agent_qualified
    async def metadata(self, resource, purpose):
        if resource == "/api/version":
            return {"version": "fixture-1"}
        return {"models": [{"name": selected.model, "digest": "a" * 64, "context_length": 65536,
                            "size": 4096, "size_vram": 4096}]}
    async def show(self):
        return {"parameters": "num_ctx 65536\n"}
    monkeypatch.setattr(OllamaProvider, "_get_api_json", metadata)
    monkeypatch.setattr(OllamaProvider, "_show_model", show)
    return OllamaProvider, metadata


@pytest.mark.parametrize("stage", ["/api/version", "/api/tags", "/api/show", "/api/ps", "missing"])
def test_failed_diagnostic_revokes_qualification_including_its_response(selected, monkeypatch, stage):
    from app.providers.provider import ProviderError
    provider, normal = _diagnostic_fixture(selected, monkeypatch)
    async def metadata(self, resource, purpose):
        if resource == stage:
            raise ProviderError("fixture failure", "fixture_failure")
        if stage == "missing" and resource == "/api/tags":
            return {"models": []}
        return await normal(self, resource, purpose)
    async def show(self):
        raise ProviderError("fixture failure", "fixture_failure")
    monkeypatch.setattr(provider, "_get_api_json", metadata)
    if stage == "/api/show":
        monkeypatch.setattr(provider, "_show_model", show)
    response = asyncio.run(provider(selected).diagnostics())
    assert not resolve_effective_capabilities().file_agent_qualified
    assert not response.get("effective_capabilities", {}).get("file_agent_qualified", False)


def test_failed_invalidation_write_keeps_process_local_tombstone(selected, monkeypatch):
    from app.providers.provider import ProviderError
    import app.providers.effective_capabilities as capabilities
    provider, normal = _diagnostic_fixture(selected, monkeypatch)
    async def error(*args, **kwargs):
        raise ProviderError("fixture failure", "fixture_failure")
    with monkeypatch.context() as patch:
        patch.setattr(provider, "_get_api_json", error)
        patch.setattr(capabilities, "record_context_observation", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("busy")))
        assert asyncio.run(provider(selected).diagnostics())["status"] == "error"
        assert not resolve_effective_capabilities().file_agent_qualified
    # Readable old SQLite data cannot resurrect the failure; a later successful
    # actual metadata recording (synthetic fixture here) is required.
    assert not resolve_effective_capabilities().file_agent_qualified
    assert asyncio.run(provider(selected).diagnostics())["status"] == "ok"
    assert resolve_effective_capabilities().file_agent_qualified


def test_second_resident_confirmation_failure_cannot_reuse_first_sample(selected, monkeypatch):
    from app.providers.provider import ProviderError
    provider, normal = _diagnostic_fixture(selected, monkeypatch)
    async def metadata(self, resource, purpose):
        if purpose == "local_model_provider_confirm_running_context":
            raise ProviderError("confirmation failed", "fixture_failure")
        return await normal(self, resource, purpose)
    monkeypatch.setattr(provider, "_get_api_json", metadata)
    result = asyncio.run(provider(selected).diagnostics())
    assert result["effective_capabilities"]["deployment_hash"] is None
    assert not resolve_effective_capabilities().file_agent_qualified


def test_cancelled_diagnostic_revokes_previous_qualification(selected, monkeypatch):
    provider, _ = _diagnostic_fixture(selected, monkeypatch)
    async def scenario():
        entered = asyncio.Event()
        async def metadata(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(provider, "_get_api_json", metadata)
        task = asyncio.create_task(provider(selected).diagnostics())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert not resolve_effective_capabilities().file_agent_qualified


def test_unexpected_native_observer_error_still_revokes_qualification(selected, monkeypatch):
    import app.local_runtime.deployment_identity as deployment
    provider, _ = _diagnostic_fixture(selected, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(deployment, "service_instance", lambda _: (_ for _ in ()).throw(RuntimeError("observer fixture")))
        with pytest.raises(RuntimeError, match="observer fixture"):
            asyncio.run(provider(selected).diagnostics())
    assert not resolve_effective_capabilities().file_agent_qualified


def test_failure_after_metadata_write_cannot_leave_green_qualification(selected, monkeypatch):
    provider, _ = _diagnostic_fixture(selected, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(provider, "capabilities", lambda _: (_ for _ in ()).throw(RuntimeError("after metadata fixture")))
        with pytest.raises(RuntimeError, match="after metadata fixture"):
            asyncio.run(provider(selected).diagnostics())
    assert not resolve_effective_capabilities().file_agent_qualified


@pytest.mark.parametrize("change", ["duplicate", "mismatched_digest", "missing_digest", "confirmation_changed"])
def test_ambiguous_or_changed_resident_never_grants_qualification(selected, monkeypatch, change):
    provider, normal = _diagnostic_fixture(selected, monkeypatch)
    async def metadata(self, resource, purpose):
        payload = await normal(self, resource, purpose)
        if resource == "/api/ps":
            row = payload["models"][0]
            if change == "duplicate":
                payload["models"].append(dict(row))
            elif change == "mismatched_digest":
                row["digest"] = "b" * 64
            elif change == "missing_digest":
                row.pop("digest")
            elif purpose == "local_model_provider_confirm_running_context":
                row["size_vram"] = 0
        return payload
    monkeypatch.setattr(provider, "_get_api_json", metadata)
    result = asyncio.run(provider(selected).diagnostics())
    assert result["effective_capabilities"]["deployment_hash"] is None
    assert not resolve_effective_capabilities().file_agent_qualified


@pytest.mark.parametrize("latest_error", [True, False])
def test_late_diagnostic_cannot_restore_or_erase_newer_observation_across_endpoint_aliases(selected, monkeypatch, latest_error):
    from app.providers.provider import ProviderError
    provider, normal = _diagnostic_fixture(selected, monkeypatch)
    # Same DB identity, different spelling; never two independent generations.
    older = provider(replace(selected, base_url="http://LOCALHOST:11434"))
    newer = provider(replace(selected, base_url="http://localhost:11434"))
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def metadata(self, resource, purpose):
            if resource == "/api/version":
                if self is older:
                    entered.set()
                    await release.wait()
                    if not latest_error:
                        raise ProviderError("older error", "fixture_failure")
                elif latest_error:
                    raise ProviderError("newer error", "fixture_failure")
            return await normal(self, resource, purpose)
        monkeypatch.setattr(provider, "_get_api_json", metadata)
        pending = asyncio.create_task(older.diagnostics())
        await entered.wait()
        result = await newer.diagnostics()
        release.set()
        await pending
        return result
    response = asyncio.run(scenario())
    snapshot = resolve_effective_capabilities(configuration=newer.config)
    assert (snapshot.deployment_hash is None) == latest_error
    assert response["status"] == ("error" if latest_error else "ok")


def test_service_restart_or_observed_gpu_change_expires_imported_qualification(selected, monkeypatch):
    import app.local_runtime.deployment_identity as deployment
    from app.providers.qualification_store import import_qualification_report
    import_qualification_report(json.dumps(_reader_fixture(resolve_effective_capabilities().public())))
    before = resolve_effective_capabilities().public()
    assert before["file_agent_qualified"]
    with monkeypatch.context() as patch:
        patch.setattr(deployment, "service_instance", lambda _: {"pid": 123, "creation": "win:124"})
        after = resolve_effective_capabilities().public()
        assert after["deployment_hash"] is None and not after["file_agent_qualified"]
        assert after["identity_hash"] != before["identity_hash"]
    changed = _deployment()
    changed["resident"]["size_vram"] = 0
    # Generate the changed hash through the real observer, not a self-filled ID.
    changed = deployment.observe_deployment(selected.base_url, "fixture-1", changed["resident"], changed["service"])
    record_context_observation(selected, configured=65536, runtime=65536, model_digest="a" * 64, deployment=changed)
    after = resolve_effective_capabilities().public()
    assert after["deployment_hash"] != before["deployment_hash"]
    assert after["qualification"]["status"] == "STALE" and not after["file_agent_qualified"]


def test_public_matrix_never_exports_internal_process_binding(selected):
    from app.providers.capabilities import provider_capability_matrix
    public = provider_capability_matrix(base_url=selected.base_url, model=selected.model)
    assert "deployment" not in public["context_observation"]
    assert "win:123" not in json.dumps(public)
    assert resolve_effective_capabilities().deployment_hash


def test_valid_import_round_trip_uses_small_summary_and_digest_change_expires_it(selected, tmp_path, monkeypatch):
    from pathlib import Path
    from app.providers.qualification_store import import_qualification_report
    current = resolve_effective_capabilities().public()
    summary = import_qualification_report(json.dumps(_reader_fixture(current)))
    assert summary["status"] == "PASS"
    assert len(list((tmp_path / "model-qualifications").glob("*.report.json"))) == 1
    original_open = Path.open
    def bounded_read(path, *args, **kwargs):
        if path.name.endswith(".report.json"):
            pytest.fail("profile must never rescan the full report")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", bounded_read)
    snapshot = resolve_effective_capabilities()
    assert snapshot.file_agent_qualified
    assert snapshot.public()["qualification"]["levels"]["file_agent"]["qualified"]
    record_context_observation(selected, configured=65536, runtime=65536, model_digest="b" * 64)
    stale = resolve_effective_capabilities().public()
    assert stale["qualification"]["status"] == "STALE"
    assert not stale["file_agent_qualified"]


def test_partial_model_can_keep_basic_without_file_agent(selected):
    from app.providers.qualification_store import import_qualification_report
    report = _reader_fixture(resolve_effective_capabilities().public())
    report["status"] = "failed"
    for row in report["case_results"]:
        if row["case_id"] != "basic-chat":
            row["status"] = "failed"
    result = import_qualification_report(json.dumps(report))
    assert result["levels"]["basic"]["qualified"]
    assert not result["levels"]["file_agent"]["qualified"]
    assert result["status"] == "PARTIAL"


def test_routes_return_only_current_identity_and_reject_scripted(selected):
    from fastapi import HTTPException
    from app.api.routes.local_models import qualification, import_qualification, QualificationImportInput
    response = qualification()
    assert response["effective_capabilities"]["model"] == selected.model
    assert response["qualification"]["status"] == "NOT_RUN"
    with pytest.raises(HTTPException) as error:
        import_qualification(QualificationImportInput(report_json='{"mode":"scripted"}'))
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "qualification_report_ineligible"


def test_ttl_expiry_removes_all_tiers_and_refresh_does_not_run_model(selected, monkeypatch):
    from app.providers.qualification_store import import_qualification_report
    import app.providers.capabilities as capabilities
    clock = [1000.0]
    monkeypatch.setattr(capabilities.time, "time", lambda: clock[0])
    record_context_observation(selected, configured=65536, runtime=65536, model_digest="a" * 64, ttl_seconds=10, deployment=_deployment())
    import_qualification_report(json.dumps(_reader_fixture(resolve_effective_capabilities().public())))
    assert resolve_effective_capabilities().file_agent_qualified
    clock[0] = 1009.999
    assert resolve_effective_capabilities().file_agent_qualified
    clock[0] = 1010.0
    stale = resolve_effective_capabilities().public()
    assert stale["observation_stale"]
    assert stale["qualification"]["status"] == "STALE"
    assert all(not tier["qualified"] and tier["status"] == "STALE" for tier in stale["qualification"]["levels"].values())
    # A new matching read-only metadata observation restores the original
    # imported protocol evidence; a changed runtime window still invalidates it.
    record_context_observation(selected, configured=65536, runtime=65536, model_digest="a" * 64, deployment=_deployment())
    assert resolve_effective_capabilities().file_agent_qualified
    record_context_observation(selected, configured=65536, runtime=4096, model_digest="a" * 64)
    assert resolve_effective_capabilities().qualification["status"] == "STALE"


def test_http_qualification_routes_require_process_auth_and_never_probe(selected, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.providers.ollama import OllamaProvider
    monkeypatch.setattr(settings, "api_token", "qualification-test-process-token")
    async def no_probe(*args, **kwargs):
        pytest.fail("qualification GET/import may not run provider diagnostics or chat")
    monkeypatch.setattr(OllamaProvider, "diagnostics", no_probe)
    monkeypatch.setattr(OllamaProvider, "chat", no_probe)
    with TestClient(app) as client:
        assert client.get("/api/local-models/qualification").status_code == 401
        assert client.post("/api/local-models/qualification/import", json={"report_json": "{}"}).status_code == 401
        assert not (tmp_path / "model-qualifications").exists()
        headers = {"X-Agent-Api-Token": "qualification-test-process-token"}
        current = client.get("/api/local-models/qualification", headers=headers)
        assert current.status_code == 200
        assert current.json()["qualification"]["status"] == "NOT_RUN"
        imported = client.post("/api/local-models/qualification/import", headers=headers,
                               json={"report_json": json.dumps(_reader_fixture(current.json()["effective_capabilities"]))})
        assert imported.status_code == 200
        assert imported.json()["qualification"]["status"] == "PASS"
        assert selected.base_url not in imported.text
        assert str(tmp_path) not in imported.text
        # Pydantic's string bound and the importer's UTF-8 byte bound are
        # independent; neither may modify a previously imported summary.
        before = (tmp_path / "model-qualifications" / (current.json()["effective_capabilities"]["configuration_hash"] + ".json")).read_bytes()
        oversized_ascii = client.post("/api/local-models/qualification/import", headers=headers, json={"report_json": "x" * (512 * 1024 + 1)})
        assert oversized_ascii.status_code == 422
        oversized_utf8 = client.post("/api/local-models/qualification/import", headers=headers, json={"report_json": "中" * (200 * 1024)})
        assert oversized_utf8.status_code == 409
        assert oversized_utf8.json()["detail"]["code"] == "qualification_report_too_large"
        assert (tmp_path / "model-qualifications" / (current.json()["effective_capabilities"]["configuration_hash"] + ".json")).read_bytes() == before
