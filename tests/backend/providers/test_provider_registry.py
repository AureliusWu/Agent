from __future__ import annotations

import asyncio
import json

import pytest

from app.providers.base import FailureCategory
from app.providers.configuration import (
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    ProviderConfiguration,
    load_provider_configuration,
    save_provider_configuration,
    validate_provider_configuration,
)
from app.providers.mock import MOCK_SCENARIOS, MockProvider
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
