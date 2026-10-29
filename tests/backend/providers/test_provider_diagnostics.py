from __future__ import annotations

import asyncio
import json
import socket
import ssl
from dataclasses import asdict

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.providers.configuration import ProviderConfiguration, validate_provider_configuration
from app.providers.diagnostics import connection_error, discover_models
from app.providers.model_routing import apply_manual_override, model_for_tier, route_for_tier
from app.security.network_security import NetworkPolicyError


class OfflineClient:
    def __init__(self, **kwargs) -> None:
        assert kwargs["follow_redirects"] is False
        assert 1 <= kwargs["timeout"] <= 30

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.fixture(autouse=True)
def isolated_provider_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr("app.providers.diagnostics.httpx.AsyncClient", OfflineClient)


def test_discovery_checks_unsaved_draft_without_persistence_or_generation(monkeypatch, tmp_path) -> None:
    path = tmp_path / "provider.json"
    original = json.dumps(asdict(ProviderConfiguration(provider_id="mock"))).encode()
    path.write_bytes(original)
    captured: list[tuple] = []

    async def guarded(_client, method, url, **kwargs):
        captured.append((method, url, kwargs))
        return httpx.Response(200, request=httpx.Request(method, url), json={"data": [
            {"id": "deepseek-flash", "name": "DeepSeek Flash"}, {"id": "deepseek-v4-pro"},
        ]})

    monkeypatch.setattr("app.providers.diagnostics.guarded_request", guarded)
    result = asyncio.run(discover_models(ProviderConfiguration(provider_id="deepseek", base_url="https://api.deepseek.com/v1", model="deepseek-flash"), "synthetic-model-key"))

    assert result["status"] == "ok"
    assert result["models"] == [{"id": "deepseek-flash", "name": "DeepSeek Flash"}, {"id": "deepseek-v4-pro", "name": "deepseek-v4-pro"}]
    assert result["selected_model_available"] is True
    assert result["connection_scope"] == "model_catalog_only" and result["chat_verified"] is False
    assert len(captured) == 1
    assert captured[0][0:2] == ("GET", "https://api.deepseek.com/v1/models")
    assert captured[0][2]["headers"] == {"Authorization": "Bearer synthetic-model-key"}
    assert captured[0][2]["allow_private"] is False
    assert captured[0][2]["allow_redirects"] is False
    assert path.read_bytes() == original
    assert "synthetic-model-key" not in json.dumps(result)


@pytest.mark.parametrize("provider_id,endpoint,resource,payload", [
    ("ollama", "http://127.0.0.1:11434", "/api/tags", {"models": [{"name": "qwen3:4b"}]}),
    ("openai_compatible", "http://127.0.0.1:1234/v1", "/models", {"data": [{"id": "local-model"}]}),
])
def test_local_discovery_works_without_selected_model_and_never_sends_cloud_secret(monkeypatch, provider_id, endpoint, resource, payload) -> None:
    async def guarded(_client, method, url, **kwargs):
        assert method == "GET" and url == endpoint + resource
        assert kwargs["allow_private"] is True
        assert kwargs["headers"] == {}
        return httpx.Response(200, request=httpx.Request(method, url), json=payload)

    monkeypatch.setattr("app.providers.diagnostics.guarded_request", guarded)
    monkeypatch.setattr(settings, "deepseek_api_key", "environment-cloud-secret")
    result = asyncio.run(discover_models(ProviderConfiguration(provider_id=provider_id, base_url=endpoint), "supplied-cloud-secret"))
    assert result["status"] == "ok" and len(result["models"]) == 1
    assert result["model"] == "" and result["selected_model_available"] is None
    assert "secret" not in json.dumps(result)


def test_remote_discovery_requires_key_without_request(monkeypatch) -> None:
    async def forbidden(*_args, **_kwargs):
        pytest.fail("Missing credentials must not send a request")

    monkeypatch.setattr("app.providers.diagnostics.guarded_request", forbidden)
    result = asyncio.run(discover_models(ProviderConfiguration()))
    assert result["status"] == "unconfigured"
    assert result["error_type"] == "missing_api_key" and result["models"] == []


def test_custom_remote_does_not_reuse_deepseek_environment_key(monkeypatch) -> None:
    async def forbidden(*_args, **_kwargs):
        pytest.fail("A DeepSeek environment key must not be sent to another provider")

    monkeypatch.setattr(settings, "deepseek_api_key", "synthetic-deepseek-environment-key")
    monkeypatch.setattr("app.providers.diagnostics.guarded_request", forbidden)
    result = asyncio.run(discover_models(ProviderConfiguration(provider_id="openai_compatible", base_url="https://models.example.test/v1", model="example-model")))
    assert result["status"] == "unconfigured" and result["error_type"] == "missing_api_key"


def test_legacy_health_and_completion_do_not_reuse_deepseek_environment_key(monkeypatch) -> None:
    from app.providers.provider import ProviderError, completion, provider_health

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Wrong-audience environment credentials must not reach the network")

    monkeypatch.setattr(settings, "deepseek_api_key", "synthetic-deepseek-environment-key")
    monkeypatch.setattr("app.providers.provider.guarded_request", forbidden)
    target = "https://models.example.test/v1"
    assert asyncio.run(provider_health(base_url=target, model="example-model"))["status"] == "unconfigured"
    with pytest.raises(ProviderError) as caught:
        asyncio.run(completion([{"role": "user", "content": "synthetic"}], base_url=target, model="example-model"))
    assert caught.value.error_type == "missing_api_key"


def test_ready_requires_custom_provider_credential_not_legacy_environment(monkeypatch) -> None:
    from app.providers.openai_compatible import OpenAICompatibleProvider
    from app.providers.registry import provider_ready

    target = OpenAICompatibleProvider(ProviderConfiguration(provider_id="openai_compatible", base_url="https://models.example.test/v1", model="example-model"))
    monkeypatch.setattr(settings, "deepseek_api_key", "synthetic-deepseek-environment-key")
    monkeypatch.setattr("app.providers.registry.get_provider", lambda: target)
    assert provider_ready() is False
    assert provider_ready("synthetic-explicit-custom-key") is True


def test_mock_discovery_is_in_process(monkeypatch) -> None:
    async def forbidden(*_args, **_kwargs):
        pytest.fail("Mock discovery must not send a request")

    monkeypatch.setattr("app.providers.diagnostics.guarded_request", forbidden)
    result = asyncio.run(discover_models(ProviderConfiguration(provider_id="mock"), "ignored-cloud-secret"))
    assert result["models"][0]["id"] == "siyi-mock-v1"
    assert result["chat_verified"] is False


def test_catalogue_is_bounded_deduplicated_and_redacts_untrusted_names(monkeypatch) -> None:
    secret = "sk-" + "synthetic-only-no-real-key"

    async def guarded(_client, method, url, **_kwargs):
        return httpx.Response(200, request=httpx.Request(method, url), json={"data": [
            {"id": "model-a", "name": secret}, {"id": "model-a"},
            {"id": "../escape"}, {"id": secret}, {"id": 123},
        ] + [{"id": f"model-{i}"} for i in range(600)]})

    monkeypatch.setattr("app.providers.diagnostics.guarded_request", guarded)
    result = asyncio.run(discover_models(ProviderConfiguration(), secret))
    assert result["status"] == "ok" and result["truncated"] is True
    assert len(result["models"]) <= 512
    assert len({item["id"] for item in result["models"]}) == len(result["models"])
    assert secret not in json.dumps(result) and "../escape" not in json.dumps(result)
    assert result["selected_model_available"] is None


def test_directory_success_does_not_claim_selected_model_available(monkeypatch) -> None:
    async def guarded(_client, method, url, **_kwargs):
        return httpx.Response(200, request=httpx.Request(method, url), json={"data": [{"id": "other-model"}]})

    monkeypatch.setattr("app.providers.diagnostics.guarded_request", guarded)
    result = asyncio.run(discover_models(ProviderConfiguration(model="not-listed"), "synthetic-model-key"))
    assert result["status"] == "ok" and result["selected_model_available"] is False
    assert result["chat_verified"] is False


@pytest.mark.parametrize("payload", [{}, {"data": {}}, [], {"data": None}])
def test_invalid_directory_is_not_reported_as_connected(monkeypatch, payload) -> None:
    async def guarded(_client, method, url, **_kwargs):
        return httpx.Response(200, request=httpx.Request(method, url), json=payload)

    monkeypatch.setattr("app.providers.diagnostics.guarded_request", guarded)
    result = asyncio.run(discover_models(ProviderConfiguration(), "synthetic-model-key"))
    assert result["status"] == "error" and result["error_type"] == "invalid_response"
    assert result["models"] == [] and result["chat_verified"] is False


@pytest.mark.parametrize("status,error_type", [(401,"authentication"), (402,"quota_exhausted"), (403,"access_denied"), (404,"endpoint_not_found"), (429,"rate_limited"), (302,"redirect_blocked"), (503,"service_unavailable"), (400,"http_error")])
def test_http_errors_are_safe_typed_and_not_retried(monkeypatch, status, error_type) -> None:
    calls = []

    async def guarded(_client, method, url, **_kwargs):
        calls.append(url)
        return httpx.Response(status, request=httpx.Request(method, url), json={"error": "Authorization: Bearer " + "sk-" + "do-not-return-this-synthetic-key"})

    monkeypatch.setattr("app.providers.diagnostics.guarded_request", guarded)
    result = asyncio.run(discover_models(ProviderConfiguration(max_retries=5), "synthetic-model-key"))
    assert result["status"] == "error" and result["error_type"] == error_type
    assert result["http_status"] == status and len(calls) == 1
    assert "sk-do-not" not in json.dumps(result) and "synthetic-model-key" not in json.dumps(result)


@pytest.mark.parametrize("error,error_type", [
    (FileNotFoundError(2, "private/path " + "sk-" + "synthetic-do-not-expose"), "runtime_file_missing"),
    (socket.gaierror(-1, "sensitive-host"), "dns_error"),
    (ssl.SSLError("sensitive certificate"), "tls_error"),
    (httpx.ConnectError("certificate verify failed secret"), "tls_error"),
    (httpx.ConnectError("sensitive endpoint"), "connection_error"),
    (httpx.ConnectTimeout("sensitive endpoint"), "timeout"),
    (NetworkPolicyError("Cannot resolve outbound host: sensitive-host"), "dns_error"),
    (NetworkPolicyError("Blocked outbound host: sensitive-host"), "network_policy"),
    (NetworkPolicyError("Redirects are disabled for this outbound request"), "redirect_blocked"),
    (ValueError("sensitive body"), "invalid_response"),
])
def test_connection_error_classification_does_not_return_sensitive_details(monkeypatch, error, error_type) -> None:
    async def guarded(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("app.providers.diagnostics.guarded_request", guarded)
    result = asyncio.run(discover_models(ProviderConfiguration(), "synthetic-model-key"))
    assert result["error_type"] == error_type
    assert "sensitive" not in result["error"] and "private/path" not in result["error"]


def test_wrapped_dns_error_is_classified() -> None:
    wrapped = httpx.ConnectError("network failure")
    wrapped.__cause__ = socket.gaierror(-1, "untrusted host")
    assert connection_error(wrapped)[0] == "dns_error"


def test_legacy_health_reports_runtime_file_missing_without_sensitive_error(monkeypatch) -> None:
    from app.providers.provider import provider_health

    observed = []

    class BrokenRuntimeClient:
        def __init__(self, **_kwargs):
            raise FileNotFoundError(2, "missing private/path " + "sk-" + "synthetic-secret-forbidden")

    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", BrokenRuntimeClient)
    monkeypatch.setattr("app.providers.provider.record_provider_observation", lambda **kwargs: observed.append(kwargs))
    monkeypatch.setattr("app.providers.provider.provider_capability_matrix", lambda **_kwargs: {})
    result = asyncio.run(provider_health("synthetic-model-key"))
    assert result["status"] == "error" and result["error_type"] == "runtime_file_missing"
    assert "程序缺少 HTTPS" in result["error"]
    assert "private/path" not in json.dumps(result) and "sk-synthetic" not in json.dumps(result)
    assert observed[0]["error"] == result["error"]


@pytest.mark.parametrize("path", ["/chat/completions", "/v1/chat/completions", "/models", "/v1/../v1", "/v2"])
def test_deepseek_configuration_rejects_full_api_paths(path) -> None:
    with pytest.raises(ValueError, match="基础地址"):
        validate_provider_configuration(ProviderConfiguration(base_url="https://api.deepseek.com" + path))


@pytest.mark.parametrize("endpoint", ["https://api.deepseek.com", "https://api.deepseek.com/", "https://api.deepseek.com/v1", "https://api.deepseek.com/v1/"])
def test_deepseek_configuration_accepts_official_base_paths(endpoint) -> None:
    assert validate_provider_configuration(ProviderConfiguration(base_url=endpoint)).base_url == endpoint


def test_invalid_deepseek_port_error_does_not_echo_user_input() -> None:
    with pytest.raises(ValueError) as caught:
        validate_provider_configuration(ProviderConfiguration(base_url="https://api.deepseek.com:private-value"))
    assert "private-value" not in str(caught.value)


def test_explicit_saved_deepseek_model_beats_environment_tier_defaults(monkeypatch) -> None:
    config = ProviderConfiguration(model="deepseek-v4-pro")
    monkeypatch.setattr("app.providers.model_routing.load_provider_configuration", lambda: config)
    monkeypatch.setattr(settings, "model_light_name", "deepseek-flash")
    monkeypatch.setattr(settings, "model_medium_name", "deepseek-flash")
    monkeypatch.setattr(settings, "model_strong_name", "deepseek-flash")
    assert [model_for_tier(tier) for tier in ("light", "medium", "strong")] == ["deepseek-v4-pro"] * 3
    route = route_for_tier("light", task_type="test", confidence=1.0, reason="test")
    assert apply_manual_override(route, preferred_model="deepseek-flash").model == "deepseek-flash"


def test_draft_models_api_preserves_configuration_and_process_auth(monkeypatch, tmp_path) -> None:
    from app.main import app

    path = tmp_path / "provider.json"
    original = json.dumps(asdict(ProviderConfiguration(provider_id="mock"))).encode()
    path.write_bytes(original)
    monkeypatch.setattr(settings, "api_token", "synthetic-process-token")
    with TestClient(app) as client:
        denied = client.post("/api/provider/models", json={"provider_id": "mock"})
        assert denied.status_code == 401
        accepted = client.post("/api/provider/models", json={"provider_id": "mock"}, headers={"X-Agent-Api-Token": "synthetic-process-token", "X-Model-Api-Key": "ignored-cloud-secret"})
    assert accepted.status_code == 200 and accepted.json()["models"][0]["id"] == "siyi-mock-v1"
    assert "ignored-cloud-secret" not in accepted.text
    assert path.read_bytes() == original
