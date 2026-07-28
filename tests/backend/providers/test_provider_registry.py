from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.providers.base import FailureCategory
from app.providers.configuration import (
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    ProviderConfiguration,
    configuration_for_provider,
    load_provider_configuration,
    save_provider_configuration,
    validate_provider_configuration,
)
from app.providers.mock import MOCK_SCENARIOS, MockProvider
from app.providers.ollama import OllamaProvider
from app.providers.provider import ProviderError
from app.providers.registry import assert_paid_api_allowed, failure_category, get_provider


def test_provider_configuration_round_trip_is_isolated(monkeypatch, tmp_path) -> None:
    path = tmp_path / "provider.json"
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(path))
    expected = ProviderConfiguration(
        provider_id="ollama",
        base_url=OLLAMA_BASE_URL,
        model=OLLAMA_MODEL,
        timeout_seconds=120,
        max_tokens=4096,
    )

    assert save_provider_configuration(expected) == expected
    assert load_provider_configuration() == expected
    assert "api_key" not in json.loads(path.read_text(encoding="utf-8"))


def test_legacy_ollama_output_budget_is_normalized_on_load(monkeypatch, tmp_path) -> None:
    path = tmp_path / "provider.json"
    path.write_text(
        json.dumps(
            {
                "provider_id": "ollama",
                "base_url": OLLAMA_BASE_URL,
                "model": OLLAMA_MODEL,
                "max_tokens": 512,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(path))

    loaded = load_provider_configuration()

    assert loaded.max_tokens == 2048
    assert json.loads(path.read_text(encoding="utf-8"))["max_tokens"] == 512


def test_provider_preview_normalizes_unsaved_ollama_selection() -> None:
    preview = configuration_for_provider(
        "ollama",
        ProviderConfiguration(
            provider_id="deepseek",
            max_tokens=512,
            timeout_seconds=45,
            max_retries=1,
        ),
    )

    assert preview.provider_id == "ollama"
    assert preview.base_url == OLLAMA_BASE_URL
    assert preview.model == OLLAMA_MODEL
    assert preview.max_tokens == 2048
    assert preview.timeout_seconds == 45
    assert preview.max_retries == 1


@pytest.mark.parametrize(
    "base_url,model",
    [
        ("https://127.0.0.1:11434", OLLAMA_MODEL),
        ("http://192.168.1.10:11434", OLLAMA_MODEL),
        ("http://127.0.0.1:11435", OLLAMA_MODEL),
        (OLLAMA_BASE_URL, "qwen2:7b"),
    ],
)
def test_ollama_configuration_rejects_non_local_or_unapproved_model(base_url: str, model: str) -> None:
    with pytest.raises(ValueError):
        validate_provider_configuration(
            ProviderConfiguration(provider_id="ollama", base_url=base_url, model=model)
        )


def test_ollama_configuration_rejects_output_budget_below_safe_minimum() -> None:
    with pytest.raises(ValueError, match="不得低于 2048"):
        validate_provider_configuration(
            ProviderConfiguration(
                provider_id="ollama",
                base_url=OLLAMA_BASE_URL,
                model=OLLAMA_MODEL,
                max_tokens=2047,
            )
        )


def test_ollama_chat_applies_safe_budget_and_configured_retries(monkeypatch) -> None:
    captured: dict = {}

    async def completion(*args, **kwargs):
        captured.update(kwargs)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr("app.providers.ollama.transport_completion", completion)
    target = OllamaProvider(
        ProviderConfiguration(
            provider_id="ollama",
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_MODEL,
            max_tokens=4096,
            max_retries=4,
        )
    )

    asyncio.run(target.chat([{"role": "user", "content": "hi"}], max_tokens=32))

    assert captured["max_tokens"] == 2048
    assert captured["max_retries"] == 4


class OllamaResponse:
    def __init__(self, body=None, *, status_code: int = 200, invalid_json: bool = False) -> None:
        self.body = body
        self.status_code = status_code
        self.invalid_json = invalid_json

    def json(self):
        if self.invalid_json:
            raise ValueError("not json")
        return self.body


def ollama_provider() -> OllamaProvider:
    return OllamaProvider(
        ProviderConfiguration(
            provider_id="ollama",
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_MODEL,
        )
    )


def test_ollama_diagnostics_lists_installed_models(monkeypatch) -> None:
    async def guarded(_client, _method, url, **_kwargs):
        if url.endswith("/api/version"):
            return OllamaResponse({"version": "0.32.5"})
        return OllamaResponse(
            {
                "models": [
                    {
                        "name": OLLAMA_MODEL,
                        "size": 2_500_000_000,
                        "modified_at": "2026-07-28T00:00:00Z",
                    }
                ]
            }
        )

    monkeypatch.setattr("app.providers.ollama.guarded_request", guarded)
    result = asyncio.run(ollama_provider().diagnostics())

    assert result["status"] == "ok"
    assert result["version"] == "0.32.5"
    assert result["models"][0]["name"] == OLLAMA_MODEL
    assert "首次加载" in result["first_load_hint"]


def test_ollama_diagnostics_reports_missing_model_without_download(monkeypatch) -> None:
    async def guarded(_client, _method, url, **_kwargs):
        if url.endswith("/api/version"):
            return OllamaResponse({"version": "0.32.5"})
        return OllamaResponse({"models": []})

    monkeypatch.setattr("app.providers.ollama.guarded_request", guarded)
    result = asyncio.run(ollama_provider().diagnostics())

    assert result["status"] == "error"
    assert result["error_type"] == "ollama_model_missing"
    assert result["action"] == f"ollama pull {OLLAMA_MODEL}"
    assert "自动下载" in result["error"]


def test_ollama_diagnostics_reports_service_and_port_errors(monkeypatch) -> None:
    async def offline(_client, _method, url, **_kwargs):
        raise httpx.ConnectError("offline", request=httpx.Request("GET", url))

    monkeypatch.setattr("app.providers.ollama.guarded_request", offline)
    unavailable = asyncio.run(ollama_provider().diagnostics())
    assert unavailable["error_type"] == "ollama_service_unavailable"
    assert "打开" in unavailable["error"]

    async def occupied(_client, _method, _url, **_kwargs):
        return OllamaResponse(invalid_json=True)

    monkeypatch.setattr("app.providers.ollama.guarded_request", occupied)
    conflict = asyncio.run(ollama_provider().diagnostics())
    assert conflict["error_type"] == "ollama_port_conflict"
    assert "占用" in conflict["error"]


def test_registry_defaults_to_deepseek(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "missing.json"))
    assert get_provider().id == "deepseek"


def test_invalid_persisted_configuration_fails_closed(monkeypatch, tmp_path) -> None:
    path = tmp_path / "provider.json"
    path.write_text(
        '{"provider_id":"ollama","base_url":"http://remote.example:11434","model":"qwen3:4b"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(path))
    with pytest.raises(ValueError, match="拒绝自动回退"):
        get_provider()


def test_paid_api_guard_requires_both_switches(monkeypatch) -> None:
    monkeypatch.delenv("SIYI_TEST_PROVIDER", raising=False)
    monkeypatch.delenv("SIYI_ALLOW_PAID_API", raising=False)
    with pytest.raises(ProviderError) as blocked:
        assert_paid_api_allowed()
    assert blocked.value.error_type == "paid_api_blocked"

    monkeypatch.setenv("SIYI_TEST_PROVIDER", "deepseek")
    with pytest.raises(ProviderError):
        assert_paid_api_allowed()

    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "true")
    assert_paid_api_allowed()


def test_failure_categories_are_stable() -> None:
    assert failure_category(ProviderError("bad", "invalid_json")) == FailureCategory.PROTOCOL_FAILURE
    assert failure_category(ProviderError("bad", "invalid_tool_call")) == FailureCategory.TOOL_FAILURE
    assert failure_category(ProviderError("bad", "timeout")) == FailureCategory.RUNTIME_FAILURE
    assert failure_category(ProviderError("bad", "authentication")) == FailureCategory.ENVIRONMENT_FAILURE
    assert failure_category(ProviderError("bad", "server_error")) == FailureCategory.MODEL_FAILURE


@pytest.mark.parametrize("scenario", MOCK_SCENARIOS)
def test_all_mock_scenarios_are_constructible(scenario: str) -> None:
    assert MockProvider(scenario).scenario == scenario


def test_mock_normal_stream_tool_and_structured_output() -> None:
    normal = asyncio.run(MockProvider("normal").chat([{"role": "user", "content": "hello"}]))
    assert normal["content"] == "mock response"

    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    streamed = asyncio.run(
        MockProvider("stream").chat(
            [{"role": "user", "content": "hello"}],
            event_callback=emit,
        )
    )
    assert streamed["content"] == "mock response"
    assert "".join(item[1]["delta"] for item in events) == "mock response"

    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    tool = asyncio.run(MockProvider("tool_call").tool_call([], tools))
    assert tool["tool_calls"][0]["function"]["name"] == "read_file"

    structured = asyncio.run(MockProvider("structured_output").structured_output([], schema={"type": "object"}))
    assert structured == {"status": "ok", "provider": "mock"}


@pytest.mark.parametrize(
    "scenario,error_type",
    [
        ("model_error", "server_error"),
        ("timeout", "timeout"),
        ("empty_response", "empty_response"),
        ("stream_disconnect", "network_error"),
        ("context_overflow", "context_overflow"),
    ],
)
def test_mock_failure_scenarios_execute(scenario: str, error_type: str) -> None:
    with pytest.raises(ProviderError) as raised:
        asyncio.run(MockProvider(scenario).chat([]))
    assert raised.value.error_type == error_type


def test_mock_cancelled_scenario_executes() -> None:
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(MockProvider("cancelled").chat([]))


def test_mock_health_error_is_environment_failure() -> None:
    health = asyncio.run(MockProvider("health_error").health_check())
    assert health["status"] == "error"
    assert health["failure_category"] == FailureCategory.ENVIRONMENT_FAILURE.value


def test_mock_invalid_json_executes_through_structured_output_contract() -> None:
    with pytest.raises(ProviderError) as raised:
        asyncio.run(MockProvider("invalid_json").structured_output([]))
    assert raised.value.error_type == "invalid_json"


@pytest.mark.parametrize(
    "scenario,expected_name,expected_arguments,expected_count",
    [
        ("missing_tool_arguments", "read_file", "", 1),
        ("invalid_tool_arguments", "read_file", "{invalid", 1),
        ("unknown_tool", "unknown_tool", '{"path": "README.md"}', 1),
        ("duplicate_tool_call", "read_file", '{"path": "README.md"}', 2),
    ],
)
def test_mock_tool_protocol_scenarios_execute(
    scenario: str,
    expected_name: str,
    expected_arguments: str,
    expected_count: int,
) -> None:
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    response = asyncio.run(MockProvider(scenario).chat([], tools=tools))
    calls = response["tool_calls"]
    assert len(calls) == expected_count
    assert calls[0]["function"]["name"] == expected_name
    assert calls[0]["function"]["arguments"] == expected_arguments


def test_mock_truncated_output_executes() -> None:
    response = asyncio.run(MockProvider("truncated_output").chat([]))
    assert response["content"] == "partial"
    assert response["finish_reason"] == "length"
