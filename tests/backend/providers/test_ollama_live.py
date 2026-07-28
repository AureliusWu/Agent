from __future__ import annotations

import asyncio
import os

import pytest

from app.providers.configuration import OLLAMA_BASE_URL, OLLAMA_MODEL, ProviderConfiguration
from app.providers.ollama import OllamaProvider


pytestmark = [
    pytest.mark.local_model,
    pytest.mark.skipif(os.getenv("SIYI_TEST_PROVIDER") != "ollama", reason="requires explicit Ollama test selection"),
]


def provider() -> OllamaProvider:
    return OllamaProvider(
        ProviderConfiguration(
            provider_id="ollama",
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_MODEL,
            timeout_seconds=120,
            max_tokens=1024,
        )
    )


def test_ollama_health_and_plain_chat() -> None:
    target = provider()
    health = asyncio.run(target.health_check())
    assert health["status"] == "ok", health
    response = asyncio.run(
        target.chat(
            [
                {"role": "system", "content": "Answer with exactly LOCAL_OK and nothing else."},
                {"role": "user", "content": "Confirm local execution."},
            ],
            max_tokens=512,
        )
    )
    assert "LOCAL_OK" in str(response.get("content") or "")


def test_ollama_real_streaming() -> None:
    events: list[str] = []

    async def emit(event: str, data: dict) -> None:
        if event == "model.delta":
            events.append(str(data.get("delta") or ""))

    response = asyncio.run(
        provider().chat(
            [
                {"role": "system", "content": "Answer with exactly STREAM_OK and nothing else."},
                {"role": "user", "content": "Stream the answer."},
            ],
            event_callback=emit,
            max_tokens=512,
        )
    )
    assert events
    assert "STREAM_OK" in str(response.get("content") or "")


def test_ollama_real_multi_turn_tool_call() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_temperature",
                "description": "Look up the current temperature for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    messages = [
        {
            "role": "system",
            "content": "You must call lookup_temperature for weather questions. Do not guess.",
        },
        {"role": "user", "content": "What is the temperature in Shanghai?"},
    ]
    target = provider()
    first = asyncio.run(target.tool_call(messages, tools, max_tokens=256))
    calls = first.get("tool_calls") or []
    assert calls, first
    assert calls[0]["function"]["name"] == "lookup_temperature"

    messages.extend(
        [
            {key: value for key, value in first.items() if not key.startswith("_")},
            {
                "role": "tool",
                "tool_call_id": calls[0]["id"],
                "name": "lookup_temperature",
                "content": '{"city":"Shanghai","temperature_c":28}',
            },
        ]
    )
    final = asyncio.run(target.chat(messages, tools=tools, max_tokens=512))
    assert "28" in str(final.get("content") or ""), final
