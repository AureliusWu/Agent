from __future__ import annotations

import asyncio
import os
import time

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
            max_tokens=4096,
        )
    )


def test_ollama_health_and_plain_chat() -> None:
    target = provider()
    health = asyncio.run(target.health_check())
    assert health["status"] == "ok", health
    response = asyncio.run(
        target.chat(
            [
                {"role": "system", "content": "请用一句简短中文回答。"},
                {"role": "user", "content": "确认本地模型正在正常工作。"},
            ],
            max_tokens=2048,
        )
    )
    assert str(response.get("content") or "").strip()


def test_ollama_ten_turn_conversation_has_no_empty_content() -> None:
    target = provider()
    messages: list[dict] = [
        {
            "role": "system",
            "content": "请简洁回答每一个算术问题。",
        }
    ]
    for index in range(1, 11):
        expected = str(index * 2)
        messages.append({"role": "user", "content": f"{index}+{index} 等于多少？"})
        response = asyncio.run(target.chat(messages, max_tokens=2048))
        assert expected in str(response.get("content") or ""), response
        messages.append({key: value for key, value in response.items() if not key.startswith("_")})


def test_ollama_real_streaming_ten_times_without_empty_content() -> None:
    target = provider()
    for index in range(1, 11):
        events: list[str] = []

        async def emit(event: str, data: dict) -> None:
            if event == "model.delta":
                events.append(str(data.get("delta") or ""))

        expected = str(index + 10)
        response = asyncio.run(
            target.chat(
                [
                    {"role": "system", "content": "请用一句话简洁回答算术问题。"},
                    {"role": "user", "content": f"{index}+10 等于多少？"},
                ],
                event_callback=emit,
                max_tokens=2048,
            )
        )
        assert events, response
        assert expected in str(response.get("content") or ""), response
        assert response["_metrics"]["provider"] == "ollama"
        assert response["_metrics"]["model"] == OLLAMA_MODEL
        assert response["_metrics"]["first_token_ms"] is not None
        assert response["_metrics"]["first_token_ms"] <= response["_metrics"]["latency_ms"]


def test_ollama_real_multi_turn_tool_call_ten_times() -> None:
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
    target = provider()
    for index in range(1, 11):
        temperature = 20 + index
        messages = [
            {
                "role": "system",
                "content": "You must call lookup_temperature for weather questions. Do not guess.",
            },
            {"role": "user", "content": "What is the temperature in Shanghai?"},
        ]
        first = asyncio.run(target.tool_call(messages, tools, max_tokens=2048))
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
                    "content": f'{{"city":"Shanghai","temperature_c":{temperature}}}',
                },
            ]
        )
        final = asyncio.run(target.chat(messages, tools=tools, max_tokens=2048))
        assert str(temperature) in str(final.get("content") or ""), final


def test_ollama_in_flight_request_cancels_within_three_seconds() -> None:
    async def scenario() -> float:
        target = provider()
        task = asyncio.create_task(
            target.chat(
                [
                    {
                        "role": "user",
                        "content": "Write a detailed 3000-word technical essay about distributed systems.",
                    }
                ],
                event_callback=lambda _event, _data: None,
                max_tokens=2048,
            )
        )
        await asyncio.sleep(0.2)
        started = time.perf_counter()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.perf_counter() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 3, f"cancellation took {elapsed:.3f}s"


@pytest.mark.parametrize(
    ("capability", "prompt", "expected"),
    [
        ("classification", "只回答 A：苹果属于 A水果 还是 B交通工具？", "A"),
        ("summarization", "把“系统完成了测试并保存证据”概括为不超过8个字，必须包含“证据”。", "证据"),
        ("translation", "把“你好”翻译成英文，只输出单词。", "hello"),
        ("information_extraction", "只输出订单号：文本是‘订单号为 42，状态完成’。", "42"),
        ("boolean_reasoning", "只回答 true 或 false：2 大于 1。", "true"),
        ("code_explanation", "一句话说明 Python 的 return x 做什么，回答必须包含“返回”。", "返回"),
        ("instruction_following", "忽略其他格式，只输出大写单词 BLUE。", "BLUE"),
        ("context_recall", "记住代号 orchid。现在只输出刚才的代号。", "orchid"),
        ("arithmetic", "只输出 7+8 的数字结果。", "15"),
        ("chinese_knowledge", "中国首都是哪里？只输出城市名。", "北京"),
    ],
)
def test_ollama_capability_scenarios(capability: str, prompt: str, expected: str) -> None:
    response = asyncio.run(
        provider().chat(
            [
                {"role": "system", "content": "严格遵循用户要求，答案保持简短。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2048,
        )
    )
    content = str(response.get("content") or "").strip()
    assert expected.casefold() in content.casefold(), {"capability": capability, "content": content}
    assert response["_metrics"]["provider"] == "ollama"
