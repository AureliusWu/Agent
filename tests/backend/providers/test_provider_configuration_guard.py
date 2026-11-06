from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.routes.system import ProviderConfigurationInput, model_configuration, update_model_configuration
from app.providers.configuration import ProviderConfiguration, load_provider_configuration, provider_credential_identity
from app.providers.configuration_guard import (
    CredentialTargetConflict, configuration_update_guard, credential_request_guard,
    model_credential_target, saved_model_request,
)
from app.runtime.task_state import TERMINAL_TASK_STATUSES, TaskStatus


@pytest.fixture()
def guard_state(monkeypatch, tmp_path):
    path = tmp_path / "provider.json"
    current = ProviderConfiguration(base_url="https://api.deepseek.com", model="deepseek-flash")
    path.write_text(json.dumps(asdict(current)), encoding="utf-8")
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(path))
    database = tmp_path / "guard.db"
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE agent_tasks(status TEXT NOT NULL)")
        con.execute("CREATE TABLE conversation_queue_items(status TEXT NOT NULL)")

    @contextmanager
    def isolated_connect():
        with sqlite3.connect(database) as con:
            yield con

    monkeypatch.setattr("app.api.routes.system.connect", isolated_connect)
    monkeypatch.setattr("app.api.routes.system.audit", lambda *_args, **_kwargs: None)
    return path, database, current


def changed_target() -> ProviderConfigurationInput:
    return ProviderConfigurationInput(provider_id="openai_compatible", base_url="https://models.example.test/v1", model="example-model")


@pytest.mark.parametrize("status", [item.value for item in TaskStatus if item not in TERMINAL_TASK_STATUSES] + ["future_nonterminal_state"])
def test_credential_target_switch_rejects_all_nonterminal_tasks(guard_state, status) -> None:
    path, database, _ = guard_state
    before = path.read_bytes()
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO agent_tasks VALUES(?)", (status,))
    with pytest.raises(HTTPException) as caught:
        update_model_configuration(changed_target())
    assert caught.value.status_code == 409
    assert "任务" in caught.value.detail
    assert path.read_bytes() == before


@pytest.mark.parametrize("status", [item.value for item in TERMINAL_TASK_STATUSES])
def test_credential_target_switch_allows_terminal_tasks(guard_state, status) -> None:
    path, database, _ = guard_state
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO agent_tasks VALUES(?)", (status,))
    saved = update_model_configuration(changed_target())
    assert saved["provider_id"] == "openai_compatible"
    assert json.loads(path.read_text(encoding="utf-8"))["base_url"] == "https://models.example.test/v1"


@pytest.mark.parametrize("status", ["pending", "claimed"])
def test_credential_target_switch_rejects_queue_without_task(guard_state, status) -> None:
    path, database, _ = guard_state
    before = path.read_bytes()
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO conversation_queue_items VALUES(?)", (status,))
    with pytest.raises(HTTPException) as caught:
        update_model_configuration(changed_target())
    assert caught.value.status_code == 409 and path.read_bytes() == before


def test_same_target_model_timeout_and_deepseek_v1_changes_are_allowed(guard_state) -> None:
    _, database, _ = guard_state
    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO agent_tasks VALUES('running')")
    saved = update_model_configuration(ProviderConfigurationInput(provider_id="deepseek", base_url="https://api.deepseek.com/v1/", model="deepseek-v4-pro", timeout_seconds=120))
    assert saved["model"] == "deepseek-v4-pro" and saved["timeout_seconds"] == 120


@pytest.mark.parametrize("endpoint", ["https://api.deepseek.com", "https://API.DEEPSEEK.COM:443/", "https://api.deepseek.com/v1/"])
def test_deepseek_credential_alias_matches_desktop_algorithm(endpoint) -> None:
    configured = ProviderConfiguration(base_url=endpoint)
    expected = "model_api_key." + hashlib.sha256(b"deepseek:https://api.deepseek.com").hexdigest()
    assert model_credential_target(configured) == expected


def test_generic_alias_preserves_path_case_and_normalizes_default_port() -> None:
    configured = ProviderConfiguration(provider_id="openai_compatible", base_url="https://MODELS.example.test:443/Team/API/", model="example-model")
    expected = "model_api_key." + hashlib.sha256(b"openai_compatible:https://models.example.test/Team/API").hexdigest()
    assert model_credential_target(configured) == expected
    lower = ProviderConfiguration(provider_id="openai_compatible", base_url="https://models.example.test/team/api", model="example-model")
    assert provider_credential_identity(configured) != provider_credential_identity(lower)


def test_generic_alias_matches_desktop_idna_and_escaped_path() -> None:
    configured = ProviderConfiguration(provider_id="openai_compatible", base_url="https://例子.测试/模型/", model="example-model")
    expected = "model_api_key." + hashlib.sha256(b"openai_compatible:https://xn--fsqu00a.xn--0zwm56d/%E6%A8%A1%E5%9E%8B").hexdigest()
    assert model_credential_target(configured) == expected


@pytest.mark.parametrize("target", ["model_api_key." + "0" * 64, "not-an-alias", "non-ascii-凭据"])
def test_stale_or_invalid_target_rejected_before_request_registration(guard_state, target) -> None:
    with pytest.raises(CredentialTargetConflict):
        with credential_request_guard("/api/tasks", "POST", target):
            pytest.fail("A stale credential request must not start")
    with configuration_update_guard() as count:
        assert count == 0


@pytest.mark.parametrize("target_header", [None, "current"])
def test_inflight_submission_prevents_switch_even_before_task_is_created(guard_state, target_header) -> None:
    path, _, current = guard_state
    before = path.read_bytes()
    target = model_credential_target(current) if target_header else None
    with credential_request_guard("/api/tasks", "POST", target):
        with configuration_update_guard() as count:
            assert count == 1
        with pytest.raises(HTTPException) as caught:
            update_model_configuration(changed_target())
        assert caught.value.status_code == 409 and path.read_bytes() == before
    # A failed request releases its lease; the now-idle update is permitted.
    assert update_model_configuration(changed_target())["provider_id"] == "openai_compatible"


def test_inflight_counter_releases_on_request_exception(guard_state) -> None:
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with credential_request_guard("/api/chat", "POST", None):
            raise RuntimeError("synthetic failure")
    with configuration_update_guard() as count:
        assert count == 0


@pytest.mark.parametrize("path,method,protected", [
    ("/api/tasks", "POST", True), ("/api/chat", "POST", True),
    ("/api/tasks/task-id/resume", "POST", True), ("/api/conversations/1/compact", "POST", True),
    ("/api/provider/health", "GET", True), ("/api/tasks", "GET", False),
    ("/api/provider/models", "POST", False), ("/api/provider/configuration", "PUT", False),
    ("/api/tasks", "OPTIONS", False),
])
def test_guard_scope_excludes_explicit_draft_checks(path, method, protected) -> None:
    assert saved_model_request(path, method) is protected


def test_model_draft_can_target_another_endpoint_during_saved_request(guard_state) -> None:
    _, _, current = guard_state
    with credential_request_guard("/api/tasks", "POST", model_credential_target(current)):
        with credential_request_guard("/api/provider/models", "POST", "explicit-draft-does-not-bind-saved-target"):
            with configuration_update_guard() as count:
                assert count == 1


def test_api_middleware_rejects_stale_target_before_chat_handler(guard_state, monkeypatch) -> None:
    from app.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "api_token", "")
    reached = []

    async def no_chat(*_args, **_kwargs):
        reached.append(True)
        return {"id": "synthetic-task"}

    monkeypatch.setattr("app.api.routes.chat.submit_task", no_chat)
    client = TestClient(create_app())
    try:
        response = client.post("/api/tasks", json={"conversation_id": 1, "message": "synthetic"}, headers={"X-Model-Credential-Target": "model_api_key." + "0" * 64})
    finally:
        client.close()
    assert response.status_code == 409 and reached == []
    assert "旧密钥" in response.json()["detail"]


def test_draft_models_rejects_mismatched_key_audience_without_outbound(guard_state, monkeypatch) -> None:
    from app.config import settings
    from app.main import create_app

    path, _, current = guard_state
    before = path.read_bytes()
    monkeypatch.setattr(settings, "api_token", "")

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Mismatched credential target must never reach model discovery")

    monkeypatch.setattr("app.api.routes.system.discover_models", forbidden)
    draft = changed_target().model_dump()
    client = TestClient(create_app())
    try:
        response = client.post("/api/provider/models", json=draft, headers={"X-Model-Credential-Target": model_credential_target(current), "X-Model-Api-Key": "synthetic-wrong-target-key"})
    finally:
        client.close()
    assert response.status_code == 409 and path.read_bytes() == before
    assert "synthetic-wrong-target-key" not in response.text


def test_draft_models_matches_payload_not_saved_configuration(guard_state, monkeypatch) -> None:
    from app.config import settings
    from app.main import create_app

    path, _, _ = guard_state
    before = path.read_bytes()
    monkeypatch.setattr(settings, "api_token", "")
    captured = []

    async def discover(draft, key):
        captured.append((draft, key))
        return {"status": "ok", "models": [], "chat_verified": False}

    monkeypatch.setattr("app.api.routes.system.discover_models", discover)
    draft = ProviderConfiguration(**changed_target().model_dump())
    client = TestClient(create_app())
    try:
        response = client.post("/api/provider/models", json=asdict(draft), headers={"X-Model-Credential-Target": model_credential_target(draft), "X-Model-Api-Key": "synthetic-correct-target-key"})
    finally:
        client.close()
    assert response.status_code == 200 and captured == [(draft, "synthetic-correct-target-key")]
    assert path.read_bytes() == before


def test_legacy_health_override_cannot_send_current_key_to_another_provider(guard_state, monkeypatch) -> None:
    from app.config import settings
    from app.main import create_app

    path, _, _ = guard_state
    configured = ProviderConfiguration(**changed_target().model_dump())
    path.write_text(json.dumps(asdict(configured)), encoding="utf-8")
    monkeypatch.setattr(settings, "api_token", "")

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Provider override must not receive a key bound to the saved provider")

    monkeypatch.setattr("app.api.routes.system.provider_health", forbidden)
    client = TestClient(create_app())
    try:
        response = client.get("/api/provider/health?provider_id=deepseek", headers={"X-Model-Credential-Target": model_credential_target(configured), "X-Model-Api-Key": "synthetic-custom-provider-key"})
    finally:
        client.close()
    assert response.status_code == 409 and "synthetic-custom-provider-key" not in response.text


def test_first_modern_save_uses_read_only_legacy_snapshot_and_rejects_stale_legacy_writes(guard_state) -> None:
    path, _, _ = guard_state
    before = path.read_bytes()
    original = model_configuration()
    assert len(original["configuration_revision"]) == 32 and path.read_bytes() == before
    # An old client may still write an unversioned file. Its change must make
    # any previously captured revision stale, without an implicit migration.
    update_model_configuration(ProviderConfigurationInput(provider_id="deepseek", model="legacy-edited"))
    replacement = ProviderConfigurationInput(provider_id="deepseek", credential_generation="a" * 32)
    with pytest.raises(HTTPException) as rejected:
        update_model_configuration(replacement, original["configuration_revision"])
    assert rejected.value.status_code == 409
    selected = update_model_configuration(replacement, model_configuration()["configuration_revision"])
    saved = path.read_bytes()
    with pytest.raises(HTTPException) as rejected:
        update_model_configuration(ProviderConfigurationInput(provider_id="deepseek"))
    assert rejected.value.status_code == 409 and path.read_bytes() == saved
    assert selected["credential_generation"] == "a" * 32


def test_same_endpoint_rotation_replace_failure_preserves_active_pointer(guard_state, monkeypatch) -> None:
    path, _, current = guard_state
    old = replace(current, credential_generation="a" * 32, configuration_revision="b" * 32)
    path.write_text(json.dumps(asdict(old)), encoding="utf-8")
    before = path.read_bytes()
    selected_target = model_credential_target(old)

    def no_replace(*_args):
        raise OSError("synthetic failure before commit")

    monkeypatch.setattr(Path, "replace", no_replace)
    with pytest.raises(OSError):
        update_model_configuration(
            ProviderConfigurationInput(provider_id="deepseek", credential_generation="c" * 32),
            old.configuration_revision,
        )
    assert path.read_bytes() == before
    assert model_credential_target(load_provider_configuration()) == selected_target
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_concurrent_rotations_cas_allows_exactly_one_and_restart_readback_matches(guard_state) -> None:
    _, _, current = guard_state
    observed = model_configuration()["configuration_revision"]

    def attempt(generation):
        try:
            return update_model_configuration(
                ProviderConfigurationInput(provider_id="deepseek", credential_generation=generation),
                observed,
            )
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(2) as workers:
        first, second = list(workers.map(attempt, ("a" * 32, "b" * 32)))
    winner, loser = (first, second) if isinstance(first, dict) else (second, first)
    assert loser == 409
    assert model_configuration() == winner
    reloaded = load_provider_configuration()  # Fresh read, no in-memory transaction state.
    assert reloaded.credential_generation == winner["credential_generation"]
    assert model_credential_target(reloaded) != model_credential_target(current)
    assert reloaded.configuration_revision == winner["configuration_revision"]
    with pytest.raises(CredentialTargetConflict):
        with credential_request_guard("/api/tasks", "POST", model_credential_target(current)):
            pytest.fail("Old generation must not dispatch after rotation")
    with pytest.raises(CredentialTargetConflict):
        with credential_request_guard("/api/tasks", "POST", None):
            pytest.fail("A versioned configuration requires a bound target")


@pytest.mark.parametrize("table,status", [("agent_tasks", "waiting_user"), ("conversation_queue_items", "pending")])
def test_same_endpoint_rotation_blocks_while_tasks_or_queue_pending(guard_state, table, status) -> None:
    path, database, _ = guard_state
    before = path.read_bytes()
    with sqlite3.connect(database) as con:
        con.execute(f"INSERT INTO {table} VALUES(?)", (status,))
    with pytest.raises(HTTPException) as rejected:
        update_model_configuration(
            ProviderConfigurationInput(provider_id="deepseek", credential_generation="a" * 32),
            model_configuration()["configuration_revision"],
        )
    assert rejected.value.status_code == 409 and path.read_bytes() == before


def test_no_key_save_retains_active_generation_and_local_switch_clears_it(guard_state) -> None:
    selected = update_model_configuration(
        ProviderConfigurationInput(provider_id="deepseek", credential_generation="a" * 32),
        model_configuration()["configuration_revision"],
    )
    changed = update_model_configuration(
        ProviderConfigurationInput(provider_id="deepseek", model="other-model"),
        selected["configuration_revision"],
    )
    assert changed["credential_generation"] == selected["credential_generation"]
    with pytest.raises(HTTPException) as invalid:
        update_model_configuration(
            ProviderConfigurationInput(provider_id="mock", credential_generation=changed["credential_generation"]),
            changed["configuration_revision"],
        )
    assert invalid.value.status_code == 400
    local = update_model_configuration(ProviderConfigurationInput(provider_id="mock"), changed["configuration_revision"])
    assert local["credential_generation"] == ""


def test_generation_must_be_an_opaque_token_not_an_alias_or_secret(guard_state) -> None:
    path, _, _ = guard_state
    before = path.read_bytes()
    with pytest.raises(HTTPException) as invalid:
        update_model_configuration(
            ProviderConfigurationInput(provider_id="deepseek", credential_generation="synthetic-sensitive-draft"),
            model_configuration()["configuration_revision"],
        )
    assert invalid.value.status_code == 400
    assert "synthetic-sensitive-draft" not in invalid.value.detail and path.read_bytes() == before


def test_versioned_provider_fails_closed_without_key_and_allows_explicit_local_health(guard_state, monkeypatch) -> None:
    from app.config import settings
    from app.main import create_app
    from app.providers.registry import provider_ready

    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "deepseek_api_key", "synthetic-legacy-env-key")
    update_model_configuration(
        ProviderConfigurationInput(provider_id="deepseek", credential_generation="a" * 32),
        model_configuration()["configuration_revision"],
    )
    target = model_credential_target(load_provider_configuration())
    assert not provider_ready()

    async def forbidden(*_args, **_kwargs):
        pytest.fail("No legacy environment-key fallback or external probe")

    monkeypatch.setattr("app.providers.provider.guarded_request", forbidden)
    client = TestClient(create_app())
    try:
        assert client.get("/api/provider/health").status_code == 409
        assert client.get("/api/provider/health?provider_id=deepseek").status_code == 409
        assert client.get("/api/provider/health?provider_id=deepseek&provider_id=mock").status_code == 409
        response = client.get("/api/provider/health", headers={"X-Model-Credential-Target": target})
        assert response.status_code == 200 and response.json()["status"] == "unconfigured"
        local = client.get("/api/provider/health?provider_id=mock")
        assert local.status_code == 200 and local.json()["provider"] == "mock"
        draft = client.post("/api/provider/models", json={"provider_id": "deepseek"}, headers={"X-Model-Credential-Target": target})
        assert draft.status_code == 200 and draft.json()["status"] == "unconfigured"
        forged = client.post("/api/provider/models", json={"provider_id": "deepseek", "credential_generation": "b" * 32})
        assert forged.status_code == 409
    finally:
        client.close()
