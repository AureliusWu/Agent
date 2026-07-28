from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.providers.base import ProviderCapabilities
from app.providers.configuration import OLLAMA_BASE_URL, OLLAMA_MODEL, ProviderConfiguration
from app.providers.configuration import save_provider_configuration
from app.providers.deepseek import DeepSeekProvider
from app.providers.mock import MockProvider
from app.providers.ollama import OllamaProvider
from app.providers.provider import ProviderError
from app.providers.registry import failure_category
from app.personality.identity_service import active_identity, identity_system_context


MESSAGES = [{"role": "user", "content": "hello"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


def test_mock_exposes_the_complete_provider_contract() -> None:
    target = MockProvider()

    assert asyncio.run(target.chat(MESSAGES))["content"] == "mock response"
    assert asyncio.run(target.list_models()) == [{"name": target.model}]
    capabilities = target.capabilities()
    assert capabilities["supports_stream"] is True
    assert capabilities["supports_tools"] is True
    assert capabilities["supports_vision"] is False
    assert capabilities["supports_embeddings"] is False
    estimate = target.estimate_context(MESSAGES)
    assert estimate["context_window"] == 65_536
    assert estimate["remaining_tokens"] > 0
    assert estimate["fits"] is True


def test_stream_alias_uses_the_same_event_contract() -> None:
    async def collect() -> list[dict[str, Any]]:
        return [event async for event in MockProvider("stream").stream(MESSAGES)]

    events = asyncio.run(collect())

    assert [event["event"] for event in events] == [
        "model.delta",
        "model.delta",
        "model.completed",
    ]
    assert events[-1]["message"]["content"] == "mock response"


def test_tool_arguments_and_structured_output_are_provider_neutral() -> None:
    tool_response = asyncio.run(MockProvider("tool_call").tool_call(MESSAGES, TOOLS))
    call = tool_response["tool_calls"][0]

    assert call["type"] == "function"
    assert call["function"]["name"] == "read_file"
    assert call["function"]["arguments"] == '{"path": "README.md"}'
    structured = asyncio.run(
        MockProvider("structured_output").structured_output(
            MESSAGES,
            schema={"type": "object", "properties": {"status": {"type": "string"}}},
        )
    )
    assert structured == {"status": "ok", "provider": "mock"}


class LimitedMockProvider(MockProvider):
    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=False,
            native_tool_calls=False,
            structured_output=False,
            vision=False,
            reasoning=False,
            json_mode=False,
            embeddings=False,
            context_window=8_192,
            default_max_output_tokens=1_024,
            source="test_contract",
        )


@pytest.mark.parametrize(
    "invoke",
    [
        lambda provider: provider.tool_call(MESSAGES, TOOLS),
        lambda provider: provider.vision(MESSAGES, images=[]),
        lambda provider: provider.embedding("hello"),
        lambda provider: provider.structured_output(MESSAGES),
    ],
)
def test_unsupported_capabilities_fail_explicitly(invoke) -> None:
    with pytest.raises(ProviderError) as caught:
        asyncio.run(invoke(LimitedMockProvider()))

    assert caught.value.error_type == "unsupported_capability"
    assert failure_category(caught.value).value == "ENVIRONMENT_FAILURE"


def test_remote_provider_contract_uses_mocked_transport_only(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    async def completion(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        callback = kwargs.get("event_callback")
        if callback:
            await callback("model.delta", {"delta": "ok", "phase": "analysis"})
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr("app.providers.deepseek.transport_completion", completion)
    target = DeepSeekProvider()

    assert asyncio.run(target.chat(MESSAGES))["content"] == "ok"
    assert asyncio.run(target.tool_call(MESSAGES, TOOLS))["content"] == "ok"
    assert calls[-1]["tools"] == TOOLS
    assert target.capabilities()["supports_reasoning"] is True
    assert target.estimate_context(MESSAGES)["context_window"] >= 8_192


def test_ollama_contract_uses_configured_local_model(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def completion(messages, **kwargs):
        captured.update(kwargs)
        return {"role": "assistant", "content": "local"}

    monkeypatch.setattr("app.providers.ollama.transport_completion", completion)
    target = OllamaProvider(
        ProviderConfiguration(
            provider_id="ollama",
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_MODEL,
            max_tokens=4_096,
        )
    )
    target._apply_model_metadata(
        {
            "details": {"finetune": "Thinking"},
            "model_info": {"qwen3.context_length": 262_144},
            "capabilities": ["completion", "tools", "thinking"],
        }
    )

    assert asyncio.run(target.chat(MESSAGES))["content"] == "local"
    assert captured["provider_id_override"] == "ollama"
    assert target.capabilities()["supports_reasoning"] is True
    assert target.estimate_context(MESSAGES)["context_window"] == 262_144


def test_ollama_disabled_tools_are_rejected_even_through_chat() -> None:
    target = OllamaProvider(
        ProviderConfiguration(
            provider_id="ollama",
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_MODEL,
            allow_tools=False,
        )
    )

    with pytest.raises(ProviderError) as caught:
        asyncio.run(target.chat(MESSAGES, tools=TOOLS))

    assert caught.value.error_type == "unsupported_capability"


PROVIDER_SWITCHES = [
    ("deepseek", "ollama"),
    ("ollama", "mock"),
    ("mock", "deepseek"),
    ("deepseek", "mock"),
    ("mock", "ollama"),
    ("ollama", "deepseek"),
] * 3


@pytest.mark.parametrize(("before_provider", "after_provider"), PROVIDER_SWITCHES)
def test_provider_switch_preserves_canonical_identity_18_of_18(
    monkeypatch,
    tmp_path,
    before_provider: str,
    after_provider: str,
) -> None:
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))

    def configuration(provider_id: str) -> ProviderConfiguration:
        return ProviderConfiguration(
            provider_id=provider_id,
            base_url=OLLAMA_BASE_URL if provider_id == "ollama" else "",
            model=OLLAMA_MODEL if provider_id == "ollama" else "",
            max_tokens=4_096,
        )

    save_provider_configuration(configuration(before_provider))
    identity_before = active_identity()
    context_before = identity_system_context()
    save_provider_configuration(configuration(after_provider))

    assert active_identity() == identity_before
    assert identity_system_context() == context_before
    assert identity_before["identity"]["display_name"] == "夏目心"
