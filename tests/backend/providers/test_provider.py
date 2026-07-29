import asyncio
import ipaddress
import json
import uuid

import pytest

from app.database import init_db, rows
from app.providers.provider import ProviderError, _provider_endpoint, _rate_limit_error, completion, provider_health, provider_profile
from app.providers.registry import assert_paid_api_allowed
from app.cognition.output_protocol import UNEXECUTED_TOOL_NOTICE, parse_deepseek_text_tool_calls
from app.cognition.reasoning_summary import PRIVATE_REASONING_KEY, safe_reasoning_summary


PRIVATE_SENTINEL = "PRIVATE_CHAIN_OF_THOUGHT_SENTINEL"


@pytest.fixture(autouse=True)
def public_provider_dns(monkeypatch):
    async def resolve(_host: str, _port: int):
        return (ipaddress.ip_address("8.8.8.8"),)

    monkeypatch.setattr("app.security.network_security._resolve_host", resolve)


def test_provider_health_reports_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr("app.providers.provider.settings.deepseek_api_key", "")
    result = asyncio.run(provider_health())
    assert result["status"] == "unconfigured"
    assert result["latency_ms"] is None


class FakeResponse:
    def __init__(self, status_code: int, body=None, *, invalid_json: bool = False) -> None:
        self.status_code = status_code
        self.body = body
        self.invalid_json = invalid_json

    def json(self):
        if self.invalid_json:
            raise ValueError("bad json")
        return self.body


def test_rate_limit_classifies_temporary_throttling_and_exhausted_quota() -> None:
    temporary = _rate_limit_error(FakeResponse(429, {"error": {"type": "rate_limit_error"}}))
    exhausted = _rate_limit_error(FakeResponse(429, {"error": {"code": "insufficient_quota"}}))

    assert temporary.error_type == "rate_limited"
    assert temporary.retryable is True
    assert exhausted.error_type == "quota_exhausted"
    assert exhausted.retryable is False


class FakeClient:
    responses: list[FakeResponse] = []
    last_json = None
    last_url = None

    def __init__(self, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        self.__class__.last_url = args[0]
        self.__class__.last_json = kwargs.get("json")
        return self.responses.pop(0)


class StreamingResponse:
    status_code = 200
    headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def aiter_lines(self):
        yield f'data: {json.dumps({"choices": [{"delta": {"reasoning_content": PRIVATE_SENTINEL}}]})}'
        yield 'data: {"choices":[{"delta":{"content":"逐"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"字"}}]}'
        yield 'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}'
        yield "data: [DONE]"


class StreamingClient(FakeClient):
    def stream(self, *args, **kwargs):
        self.__class__.last_url = args[1]
        self.__class__.last_json = kwargs.get("json")
        return StreamingResponse()


class ProtocolStreamingResponse(StreamingResponse):
    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"我来查询。\\n<web_"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"search><query>今日金价</query></web_search>"}}]}'
        yield 'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5,"total_tokens":8}}'
        yield "data: [DONE]"


class ProtocolStreamingClient(FakeClient):
    def stream(self, *args, **kwargs):
        self.__class__.last_url = args[1]
        self.__class__.last_json = kwargs.get("json")
        return ProtocolStreamingResponse()


class DsmlStreamingResponse(StreamingResponse):
    async def aiter_lines(self):
        content = (
            '准备搜索。<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="web_search">'
            '<｜｜DSML｜｜parameter name="query" string="true">DeepSeek tools</｜｜DSML｜｜parameter>'
            '<｜｜DSML｜｜parameter name="count" string="false">5</｜｜DSML｜｜parameter>'
            '</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>'
        )
        yield f'data: {json.dumps({"choices": [{"delta": {"content": content}}]}, ensure_ascii=False)}'
        yield 'data: {"choices":[],"usage":{"total_tokens":8}}'
        yield "data: [DONE]"


class DsmlStreamingClient(FakeClient):
    def stream(self, *args, **kwargs):
        self.__class__.last_url = args[1]
        self.__class__.last_json = kwargs.get("json")
        return DsmlStreamingResponse()


def test_deepseek_profile_uses_current_models_without_secrets(monkeypatch) -> None:
    monkeypatch.setattr("app.providers.provider.settings.model_base_url", "https://user:pass@api.deepseek.com?token=secret")
    monkeypatch.setattr("app.providers.provider.settings.model_name", "deepseek-v4-flash")
    monkeypatch.setattr("app.providers.provider.settings.model_light_name", "deepseek-v4-flash")
    monkeypatch.setattr("app.providers.provider.settings.model_medium_name", "deepseek-v4-flash")
    monkeypatch.setattr("app.providers.provider.settings.model_strong_name", "deepseek-v4-pro")

    profile = provider_profile()

    assert profile["name"] == "DeepSeek"
    assert profile["request_url"] == "https://api.deepseek.com"
    assert profile["chat_endpoint"] == "https://api.deepseek.com/chat/completions"
    assert profile["models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert "secret" not in str(profile)
    assert "pass" not in str(profile)


def test_provider_endpoint_preserves_generic_v1_and_uses_deepseek_root() -> None:
    assert _provider_endpoint("https://api.deepseek.com", "models") == "https://api.deepseek.com/models"
    assert _provider_endpoint("https://provider.example/v1", "chat/completions") == "https://provider.example/v1/chat/completions"
    assert _provider_endpoint("https://open.bigmodel.cn/api/paas/v4", "chat/completions") == (
        "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    )
    assert _provider_endpoint("https://provider.example", "chat/completions") == "https://provider.example/v1/chat/completions"


def test_paid_api_guard_supports_an_explicit_non_default_provider(monkeypatch) -> None:
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "glm-vision")
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "true")
    assert_paid_api_allowed("glm-vision")


def test_deepseek_tool_followup_replays_native_reasoning_content(monkeypatch) -> None:
    FakeClient.responses = [
        FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "done"}}], "usage": {"total_tokens": 4}}),
    ]
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", FakeClient)
    messages = [
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": None, PRIVATE_REASONING_KEY: "native reasoning", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"success":true}'},
    ]

    result = asyncio.run(completion(messages, "secret", base_url="https://api.deepseek.com", model="deepseek-v4-pro"))

    assert result["content"] == "done"
    assert FakeClient.last_json["messages"][1]["reasoning_content"] == "native reasoning"


def test_completion_normalizes_ollama_reasoning_alias(monkeypatch) -> None:
    FakeClient.responses = [
        FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "LOCAL_OK",
                            "reasoning": PRIVATE_SENTINEL,
                        }
                    }
                ]
            },
        ),
    ]
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", FakeClient)

    result = asyncio.run(
        completion(
            [{"role": "user", "content": "test"}],
            "secret",
            base_url="https://provider.example/v1",
            model="qwen3:4b",
        )
    )

    assert result["content"] == "LOCAL_OK"
    assert "reasoning" not in result
    assert result[PRIVATE_REASONING_KEY] == PRIVATE_SENTINEL


def test_completion_retries_and_persists_usage(monkeypatch) -> None:
    init_db()
    task_id = uuid.uuid4().hex
    FakeClient.responses = [
        FakeResponse(429, {}),
        FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "完成"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 2}}),
    ]
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("app.providers.provider.settings.model_pricing_json", '{"light-model":{"input":1,"output":2}}')

    async def no_sleep(_):
        return None

    monkeypatch.setattr("app.providers.provider.asyncio.sleep", no_sleep)

    result = asyncio.run(completion(
        [{"role": "user", "content": "test"}], "secret", task_id=task_id,
        model="light-model", max_tokens=123, phase="analysis", route_tier="light", task_type="summary", route_confidence=0.9,
    ))
    recorded = rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))[0]

    assert result["content"] == "完成"
    assert result["_metrics"]["retry_count"] == 1
    assert recorded["total_tokens"] == 10
    assert recorded["cached_input_tokens"] == 5
    assert recorded["uncached_input_tokens"] == 2
    assert json.loads(recorded["price_snapshot_json"]) == {"input": 1, "output": 2}
    assert recorded["retry_count"] == 1
    assert recorded["success"] == 1
    assert recorded["route_tier"] == "light"
    assert recorded["task_type"] == "summary"
    assert recorded["max_output_tokens"] == 123
    assert recorded["estimated_cost_usd"] > 0
    assert FakeClient.last_json["max_tokens"] == 123
    assert FakeClient.last_json["thinking"] == {"type": "disabled"}
    assert FakeClient.last_url == "https://api.deepseek.com/chat/completions"


def test_deepseek_strong_route_enables_max_reasoning(monkeypatch) -> None:
    FakeClient.responses = [FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})]
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", FakeClient)

    asyncio.run(completion([{"role": "user", "content": "analyze"}], "secret", route_tier="strong"))

    assert FakeClient.last_json["thinking"] == {"type": "enabled"}
    assert FakeClient.last_json["reasoning_effort"] == "max"
    assert "temperature" not in FakeClient.last_json


def test_completion_records_invalid_json(monkeypatch) -> None:
    init_db()
    task_id = uuid.uuid4().hex
    FakeClient.responses = [FakeResponse(200, invalid_json=True)]
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", FakeClient)

    with pytest.raises(ProviderError, match="JSON"):
        asyncio.run(completion([{"role": "user", "content": "test"}], "secret", task_id=task_id))

    recorded = rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))[0]
    assert recorded["success"] == 0
    assert recorded["error_type"] == "invalid_json"


def test_completion_redacts_credentials_and_records_data_flow(monkeypatch) -> None:
    init_db()
    FakeClient.responses = [FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})]
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", FakeClient)

    asyncio.run(completion([{"role": "user", "content": "use sk-test_DO_NOT_USE_000000000000"}], "provider-secret"))

    assert "sk-test_DO_NOT_USE_000000000000" not in FakeClient.last_json["messages"][0]["content"]
    event = rows("SELECT * FROM data_flow_events ORDER BY id DESC LIMIT 1")[0]
    assert event["sink"].startswith("model_api:")
    assert event["classification"] == "credential"
    assert event["redactions"] == 1


def test_completion_blocks_cloud_metadata_endpoint(monkeypatch) -> None:
    init_db()
    FakeClient.responses = [FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "unsafe"}}]})]
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", FakeClient)

    with pytest.raises(ProviderError) as exc:
        asyncio.run(completion([{"role": "user", "content": "test"}], "secret", base_url="http://169.254.169.254"))

    assert exc.value.error_type == "network_policy"
    assert FakeClient.responses


def test_completion_streams_provider_deltas(monkeypatch, caplog) -> None:
    init_db()
    task_id = uuid.uuid4().hex
    deltas: list[str] = []
    public_reasoning: list[dict[str, str]] = []
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", StreamingClient)

    def capture_event(event: str, data: dict[str, str]) -> None:
        if event == "model.delta":
            deltas.append(str(data["delta"]))
        elif event == "reasoning.summary":
            public_reasoning.append(data)

    result = asyncio.run(
        completion(
            [{"role": "user", "content": "stream"}],
            "secret",
            task_id=task_id,
            event_callback=capture_event,
        )
    )

    assert result["content"] == "逐字"
    assert "reasoning_content" not in result
    assert result[PRIVATE_REASONING_KEY] == PRIVATE_SENTINEL
    assert deltas == ["逐字"]
    assert public_reasoning == [{"phase": "analysis", "summary": safe_reasoning_summary("analysis")}]
    assert PRIVATE_SENTINEL not in json.dumps(public_reasoning, ensure_ascii=False)
    assert PRIVATE_SENTINEL not in caplog.text
    assert result["_metrics"]["usage"]["total_tokens"] == 4
    assert result["_metrics"]["first_token_ms"] is not None
    recorded = rows("SELECT provider, model, duration_ms, first_token_ms FROM model_runs WHERE task_id=?", (task_id,))[0]
    assert recorded["first_token_ms"] is not None
    assert 0 <= recorded["first_token_ms"] <= recorded["duration_ms"]
    assert StreamingClient.last_json["stream"] is True


def test_completion_replaces_unexecuted_text_tool_protocol(monkeypatch) -> None:
    FakeClient.responses = [
        FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "<web_search><query>x</query></web_search>"}}]})
    ]
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", FakeClient)

    result = asyncio.run(completion([{"role": "user", "content": "search"}], "secret"))

    assert result["content"] == UNEXECUTED_TOOL_NOTICE
    assert "web_search" not in result["content"]


def test_streaming_protocol_guard_never_exposes_fake_tool_markup(monkeypatch) -> None:
    deltas: list[str] = []
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", ProtocolStreamingClient)

    result = asyncio.run(
        completion(
            [{"role": "user", "content": "search"}],
            "secret",
            event_callback=lambda event, data: deltas.append(str(data["delta"])) if event == "model.delta" else None,
        )
    )

    assert result["content"] == UNEXECUTED_TOOL_NOTICE
    assert "web_search" not in "".join(deltas)
    assert UNEXECUTED_TOOL_NOTICE in "".join(deltas)


def test_deepseek_dsml_fallback_becomes_validated_tool_call(monkeypatch) -> None:
    deltas: list[str] = []
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", DsmlStreamingClient)
    tools = [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "search",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                "required": ["query"],
            },
        },
    }]

    result = asyncio.run(
        completion(
            [{"role": "user", "content": "search"}],
            "secret",
            tools=tools,
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            event_callback=lambda event, data: deltas.append(str(data["delta"])) if event == "model.delta" else None,
        )
    )

    assert result["content"] == "准备搜索。"
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["function"]["name"] == "web_search"
    assert json.loads(result["tool_calls"][0]["function"]["arguments"]) == {"query": "DeepSeek tools"}
    assert "DSML" not in "".join(deltas)
    assert UNEXECUTED_TOOL_NOTICE not in "".join(deltas)


def test_dsml_parser_rejects_unexposed_tool() -> None:
    content = (
        '<｜｜DSML｜｜invoke name="dangerous_tool">'
        '<｜｜DSML｜｜parameter name="path" string="true">outside</｜｜DSML｜｜parameter>'
        '</｜｜DSML｜｜invoke>'
    )
    visible, calls = parse_deepseek_text_tool_calls(content, [])

    assert visible == content
    assert calls == []
