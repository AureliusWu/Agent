"""V16-P01: endpoint/config-scoped observations and bounded runtime budgets."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace

import pytest

from app.config import settings
from app.context.budget import model_context_profile, request_budget
from app.database import connect, rows
from app.providers.capabilities import provider_identity, record_provider_observation
from app.providers.configuration import ProviderConfiguration, save_provider_configuration
from app.providers.ollama import OllamaProvider


@pytest.fixture
def local_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))
    monkeypatch.setattr(settings, "model_context_profiles_json", "{}")
    config = ProviderConfiguration(provider_id="ollama", base_url="http://127.0.0.1:11434", model=f"v160-test:{uuid.uuid4().hex}")
    save_provider_configuration(config)
    return config


def configured_profile(config, monkeypatch, window):
    from app.providers.effective_capabilities import context_profile_key
    monkeypatch.setattr(settings, "model_context_profiles_json", json.dumps({
        context_profile_key(config): {"context_window_tokens": window},
    }))


@pytest.mark.parametrize("window", [1, 128, 512, 1024, 2048, 4096, 8191])
def test_v160_explicit_small_windows_are_never_raised(local_config, monkeypatch, window):
    configured_profile(local_config, monkeypatch, window)
    profile = model_context_profile(model=local_config.model)
    assert profile.context_window_tokens == window
    budget = request_budget([], None, model=local_config.model, desired_output_tokens=8192)
    assert budget.context_window_tokens == window
    assert budget.reserved_output_tokens + budget.provider_overhead_tokens + budget.safety_margin_tokens + budget.effective_input_budget <= window
    assert budget.reserved_output_tokens <= window
    assert budget.blocked_reason is None


def test_v160_unknown_local_window_is_explicit_and_not_file_qualified(local_config):
    from app.providers.effective_capabilities import resolve_effective_capabilities
    effective = resolve_effective_capabilities()
    assert effective.context_window_tokens is None
    assert effective.window_status == "unknown"
    assert effective.file_agent_qualified is False
    assert effective.observed_capabilities["native_tool_calls"] == "unknown"
    budget = request_budget([], None, model=local_config.model, desired_output_tokens=1000)
    assert budget.blocked_reason == "context_window_unknown"
    assert budget.effective_input_budget == 0
    assert budget.reserved_output_tokens == 0


@pytest.mark.parametrize("expiry", ["broken", "NaN", "Infinity", float("nan"), float("inf"), None, True, -1, 10**1000])
def test_corrupt_observation_expiry_is_stale_not_server_error(local_config, monkeypatch, expiry):
    import app.providers.effective_capabilities as module
    module.record_context_observation(local_config, runtime=2048, model_digest="a" * 64)
    matrix = module.provider_capability_matrix(base_url=local_config.base_url, model=local_config.model)
    matrix["context_observation"]["expires_at"] = expiry
    monkeypatch.setattr(module, "provider_capability_matrix", lambda **kwargs: matrix)
    snapshot = module.resolve_effective_capabilities(configuration=local_config)
    assert snapshot.context_window_tokens is None
    assert snapshot.observation_stale is True


@pytest.mark.parametrize("observation", [[], "broken", True, 3])
def test_non_mapping_context_observation_fails_closed(local_config, monkeypatch, observation):
    import app.providers.effective_capabilities as module
    matrix = module.provider_capability_matrix(base_url=local_config.base_url, model=local_config.model)
    matrix["context_observation"] = observation
    monkeypatch.setattr(module, "provider_capability_matrix", lambda **kwargs: matrix)
    assert module.resolve_effective_capabilities(configuration=local_config).context_window_tokens is None


@pytest.mark.parametrize("payload,expiry", [("[]", "bad"), ("{}", "NaN"), ("{}", float("inf"))])
def test_corrupt_persisted_matrix_is_stale_and_readable(local_config, payload, expiry):
    from app.providers.capabilities import provider_capability_matrix
    record_provider_observation(base_url=local_config.base_url, model=local_config.model, status="ok")
    with connect() as db:
        db.execute("UPDATE provider_capabilities SET capabilities=?,expires_at=? WHERE model=?", (payload, expiry, local_config.model))
    assert provider_capability_matrix(base_url=local_config.base_url, model=local_config.model)["stale"] is True


def test_v160_endpoint_normalization_keeps_case_sensitive_path_identity():
    assert provider_identity("HTTPS://Example.test:443/API/") == provider_identity("https://example.test/API")
    assert provider_identity("https://example.test/API") != provider_identity("https://example.test/api")
    _, secret_hash = provider_identity("https://user:secret@example.test/API?token=secret")
    provider, public_hash = provider_identity("https://example.test/API")
    assert secret_hash == public_hash
    assert provider == "example.test"


def test_v160_explicit_profiles_do_not_cross_endpoints(local_config, monkeypatch):
    from app.providers.effective_capabilities import resolve_effective_capabilities
    first = replace(local_config, provider_id="openai_compatible", base_url="http://127.0.0.1:1234/v1")
    second = replace(first, base_url="http://127.0.0.1:5678/v1")
    configured_profile(first, monkeypatch, 2048)
    a = resolve_effective_capabilities(configuration=first)
    b = resolve_effective_capabilities(configuration=second)
    assert a.context_window_tokens == 2048
    assert b.context_window_tokens is None
    assert a.identity_hash != b.identity_hash


def test_v160_legacy_model_only_local_profile_is_not_cross_endpoint_evidence(local_config, monkeypatch):
    from app.providers.effective_capabilities import resolve_effective_capabilities
    monkeypatch.setattr(settings, "model_context_profiles_json", json.dumps({local_config.model: {"context_window_tokens": 8192}}))
    assert resolve_effective_capabilities().context_window_tokens is None


def test_v160_observation_survives_provider_recreation_without_storing_endpoint(local_config):
    from app.providers.effective_capabilities import record_context_observation, resolve_effective_capabilities
    record_context_observation(local_config, theoretical=32768, configured=4096, runtime=2048, model_digest="a" * 64)
    first = resolve_effective_capabilities()
    second = resolve_effective_capabilities(configuration=replace(local_config))
    assert first.context_window_tokens == second.context_window_tokens == 2048
    assert first.theoretical_context_window == 32768
    assert first.configured_context_window == 4096
    assert first.runtime_context_window == 2048
    assert first.identity_hash == second.identity_hash
    assert first.file_agent_qualified is False
    stored = rows("SELECT * FROM provider_capabilities WHERE model=?", (local_config.model,))
    assert local_config.base_url not in json.dumps(stored)
    assert "api_key" not in json.dumps(stored)


def test_v160_theoretical_window_alone_does_not_claim_runtime_window(local_config):
    from app.providers.effective_capabilities import record_context_observation, resolve_effective_capabilities
    record_context_observation(local_config, theoretical=32768, model_digest="a" * 64)
    effective = resolve_effective_capabilities()
    assert effective.theoretical_context_window == 32768
    assert effective.context_window_tokens is None
    assert effective.window_status == "unknown"


def test_v160_changed_config_digest_and_stale_observation_do_not_reuse_identity(local_config):
    from app.providers.effective_capabilities import record_context_observation, resolve_effective_capabilities
    record_context_observation(local_config, runtime=4096, model_digest="a" * 64)
    first = resolve_effective_capabilities()
    changed = resolve_effective_capabilities(configuration=replace(local_config, max_tokens=4096))
    assert changed.context_window_tokens is None
    assert changed.identity_hash != first.identity_hash
    record_context_observation(local_config, runtime=1024, model_digest="b" * 64)
    second = resolve_effective_capabilities()
    assert second.identity_hash != first.identity_hash
    assert second.context_window_tokens == 1024
    with connect() as db:
        record = db.execute("SELECT capabilities FROM provider_capabilities WHERE model=?", (local_config.model,)).fetchone()
        data = json.loads(record[0])
        data["_context_v1"]["expires_at"] = 0
        db.execute("UPDATE provider_capabilities SET capabilities=? WHERE model=?", (json.dumps(data), local_config.model))
    assert resolve_effective_capabilities().context_window_tokens is None


def test_v160_generic_capability_observation_does_not_erase_context(local_config):
    from app.providers.effective_capabilities import record_context_observation, resolve_effective_capabilities
    record_context_observation(local_config, runtime=2048)
    record_provider_observation(base_url=local_config.base_url, model=local_config.model, status="ok", streaming=True)
    assert resolve_effective_capabilities().context_window_tokens == 2048


def test_v160_ollama_diagnostics_observes_loaded_and_configured_context(local_config, monkeypatch):
    async def get_json(self, resource, purpose):
        if resource == "/api/version":
            return {"version": "test-version"}
        return {"models": [{"name": local_config.model, "digest": "a" * 64, "context_length": 1024}]}

    async def show(self):
        return {"model_info": {"test.context_length": 32768}, "parameters": "num_ctx 2048\n", "capabilities": ["completion", "tools"]}

    monkeypatch.setattr(OllamaProvider, "_get_api_json", get_json)
    monkeypatch.setattr(OllamaProvider, "_show_model", show)
    result = asyncio.run(OllamaProvider(local_config).diagnostics())
    assert result["effective_capabilities"]["context_window_tokens"] == 1024
    assert result["effective_capabilities"]["theoretical_context_window"] == 32768
    assert model_context_profile(model=local_config.model).context_window_tokens == 1024


def test_v160_ollama_never_raises_output_above_requested_budget(local_config, monkeypatch):
    captured = {}
    async def transport(*args, **kwargs):
        captured.update(kwargs)
        return {"content": "ok"}
    monkeypatch.setattr("app.providers.ollama.transport_completion", transport)
    asyncio.run(OllamaProvider(local_config).chat([], max_tokens=32))
    assert captured["max_tokens"] == 32


def test_v160_registry_profile_exposes_shared_safe_snapshot(local_config, monkeypatch):
    from app.providers.registry import provider_profile
    from app.providers.effective_capabilities import resolve_effective_capabilities
    configured_profile(local_config, monkeypatch, 4096)
    effective = resolve_effective_capabilities()
    assert provider_profile()["effective_capabilities"]["identity_hash"] == effective.identity_hash
    assert request_budget([], None, model=local_config.model, desired_output_tokens=64).effective_identity == effective.identity_hash


@pytest.mark.parametrize("workspace", [False, True])
def test_v160_unknown_context_waits_without_calls_and_resumes_after_configuration(local_config, tmp_path, monkeypatch, workspace):
    from app.database import now_iso
    from app.runtime.runner import run_chat
    from app.schemas import ChatRequest
    root = tmp_path / "workspace"
    root.mkdir()
    task_id = uuid.uuid4().hex
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("context-test", str(root) if workspace else "", "readonly", now_iso(), now_iso()),
        )
        conversation_id = int(cursor.lastrowid)
    calls = []
    async def complete(*args, **kwargs):
        calls.append(kwargs)
        return {"content": "你好。", "usage": {"total_tokens": 1}}
    payload = ChatRequest(conversation_id=conversation_id, task_id=task_id, content="你好")
    async def run(request):
        return await asyncio.wait_for(run_chat(request, completion_fn=complete), timeout=4)
    result = asyncio.run(run(payload))
    assert result["task_status"] == "waiting_provider"
    assert calls == []
    record = rows("SELECT resumable,current_step FROM agent_tasks WHERE id=?", (task_id,))[0]
    assert record == {"resumable": 1, "current_step": "context_window_unknown"}
    assert rows("SELECT id FROM task_checkpoints WHERE task_id=?", (task_id,))
    configured_profile(local_config, monkeypatch, 65536)
    result = asyncio.run(run(payload.model_copy(update={"resume": True})))
    assert result["task_status"] != "waiting_provider"
    assert calls


def test_v160_registry_bounds_all_local_calls_including_planner(local_config, monkeypatch):
    from app.providers.registry import completion
    from app.providers.provider import ProviderError
    captured = []
    async def transport(*args, **kwargs):
        captured.append(kwargs)
        return {"content": "ok"}
    monkeypatch.setattr("app.providers.ollama.transport_completion", transport)
    configured_profile(local_config, monkeypatch, 2048)
    result = asyncio.run(completion([], max_tokens=8192, phase="planner"))
    assert result["content"] == "ok"
    assert captured[0]["max_tokens"] <= 1024
    with pytest.raises(ProviderError) as caught:
        asyncio.run(completion([{"role": "user", "content": "text" * 10000}], max_tokens=128, phase="planner"))
    assert caught.value.error_type == "context_window_exceeded"
    assert len(captured) == 1


def test_v160_digest_change_invalidates_old_boolean_observations(local_config):
    from app.providers.effective_capabilities import record_context_observation, resolve_effective_capabilities
    record_context_observation(local_config, runtime=2048, model_digest="a" * 64)
    record_provider_observation(base_url=local_config.base_url, model=local_config.model, status="ok", native_tool_calls=True)
    assert resolve_effective_capabilities().observed_capabilities["native_tool_calls"] == "supported"
    record_context_observation(local_config, runtime=1024, model_digest="b" * 64)
    assert resolve_effective_capabilities().observed_capabilities["native_tool_calls"] == "unknown"


def test_v160_mock_preview_does_not_inherit_current_local_identity(local_config):
    from app.providers.registry import provider_profile
    effective = provider_profile("mock")["effective_capabilities"]
    assert effective["provider_id"] == "mock"
    assert effective["model"] == "siyi-mock-v1"
    assert effective["window_status"] == "declared"


def test_v160_explicit_window_still_respects_lower_observed_limit(local_config, monkeypatch):
    from app.providers.effective_capabilities import record_context_observation, resolve_effective_capabilities
    configured_profile(local_config, monkeypatch, 65536)
    record_context_observation(local_config, theoretical=32768, configured=2048, runtime=4096)
    assert resolve_effective_capabilities().context_window_tokens == 2048


@pytest.mark.parametrize("mode", ["uncompressible", "observation_became_unknown"])
def test_v160_conversation_budget_wait_is_bounded_before_model_call(local_config, monkeypatch, mode):
    from app.database import now_iso
    from app.runtime import orchestration
    from app.runtime.runner import run_chat
    from app.schemas import ChatRequest
    configured_profile(local_config, monkeypatch, 128 if mode == "uncompressible" else 65536)
    with connect() as db:
        conversation_id = int(db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("bounded-context", "", "readonly", now_iso(), now_iso()),
        ).lastrowid)
    budgets = []
    real_budget = orchestration.request_budget
    def budget(*args, **kwargs):
        budgets.append(1)
        if mode == "observation_became_unknown":
            monkeypatch.setattr(settings, "model_context_profiles_json", "{}")
        return real_budget(*args, **kwargs)
    monkeypatch.setattr(orchestration, "request_budget", budget)
    async def complete(*args, **kwargs):
        pytest.fail("context-blocked conversation must not call a model")
    async def exercise():
        return await asyncio.wait_for(run_chat(ChatRequest(conversation_id=conversation_id, content="解释这个问题"), completion_fn=complete), timeout=3)
    result = asyncio.run(exercise())
    assert result["task_status"] == "waiting_provider"
    assert len(budgets) <= 3


def test_v160_model_preflight_caps_to_effective_output_budget():
    from types import SimpleNamespace
    from app.runtime.model_loop import prepare_model_call
    seen = []
    budget = SimpleNamespace(
        should_compact=False, blocked_reason=None, context_window_tokens=4096,
        estimated_input_tokens=10, provider_overhead_tokens=40, safety_margin_tokens=204,
        reserved_output_tokens=32,
    )
    def preflight(phase, tokens, maximum):
        seen.append(maximum)
        return maximum, None
    result = prepare_model_call([], [], route=SimpleNamespace(model="test", max_output_tokens=8192),
        phase="execution", token_budget=SimpleNamespace(preflight=preflight), context_budget=lambda *args, **kwargs: budget,
        compact=lambda *args, **kwargs: pytest.fail("unexpected compaction"), checkpoint=lambda *args: None,
        emit=lambda *args: None, audit=lambda *args: None, conversation_id=1, task_id="test")
    assert seen == [32]
    assert result.max_output_tokens == 32
