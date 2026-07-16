import asyncio
import ipaddress
import uuid

import pytest

from app.database import init_db, rows
from app.provider import ProviderError, _provider_endpoint, completion, provider_health, provider_profile


@pytest.fixture(autouse=True)
def public_provider_dns(monkeypatch):
    async def resolve(_host: str, _port: int):
        return (ipaddress.ip_address("8.8.8.8"),)

    monkeypatch.setattr("app.network_security._resolve_host", resolve)


def test_provider_health_reports_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr("app.provider.settings.deepseek_api_key", "")
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
        yield 'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"逐"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"字"}}]}'
        yield 'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}'
        yield "data: [DONE]"


class StreamingClient(FakeClient):
    def stream(self, *args, **kwargs):
        self.__class__.last_url = args[1]
        self.__class__.last_json = kwargs.get("json")
        return StreamingResponse()


def test_deepseek_profile_uses_current_models_without_secrets(monkeypatch) -> None:
    monkeypatch.setattr("app.provider.settings.model_base_url", "https://user:pass@api.deepseek.com?token=secret")
    monkeypatch.setattr("app.provider.settings.model_name", "deepseek-v4-flash")
    monkeypatch.setattr("app.provider.settings.model_light_name", "deepseek-v4-flash")
    monkeypatch.setattr("app.provider.settings.model_medium_name", "deepseek-v4-flash")
    monkeypatch.setattr("app.provider.settings.model_strong_name", "deepseek-v4-pro")

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
    assert _provider_endpoint("https://provider.example", "chat/completions") == "https://provider.example/v1/chat/completions"


def test_completion_retries_and_persists_usage(monkeypatch) -> None:
    init_db()
    task_id = uuid.uuid4().hex
    FakeClient.responses = [
        FakeResponse(429, {}),
        FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "完成"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}),
    ]
    monkeypatch.setattr("app.provider.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("app.provider.settings.model_pricing_json", '{"light-model":{"input":1,"output":2}}')

    async def no_sleep(_):
        return None

    monkeypatch.setattr("app.provider.asyncio.sleep", no_sleep)

    result = asyncio.run(completion(
        [{"role": "user", "content": "test"}], "secret", task_id=task_id,
        model="light-model", max_tokens=123, phase="analysis", route_tier="light", task_type="summary", route_confidence=0.9,
    ))
    recorded = rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))[0]

    assert result["content"] == "完成"
    assert result["_metrics"]["retry_count"] == 1
    assert recorded["total_tokens"] == 10
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
    monkeypatch.setattr("app.provider.httpx.AsyncClient", FakeClient)

    asyncio.run(completion([{"role": "user", "content": "analyze"}], "secret", route_tier="strong"))

    assert FakeClient.last_json["thinking"] == {"type": "enabled"}
    assert FakeClient.last_json["reasoning_effort"] == "max"
    assert "temperature" not in FakeClient.last_json


def test_completion_records_invalid_json(monkeypatch) -> None:
    init_db()
    task_id = uuid.uuid4().hex
    FakeClient.responses = [FakeResponse(200, invalid_json=True)]
    monkeypatch.setattr("app.provider.httpx.AsyncClient", FakeClient)

    with pytest.raises(ProviderError, match="JSON"):
        asyncio.run(completion([{"role": "user", "content": "test"}], "secret", task_id=task_id))

    recorded = rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))[0]
    assert recorded["success"] == 0
    assert recorded["error_type"] == "invalid_json"


def test_completion_redacts_credentials_and_records_data_flow(monkeypatch) -> None:
    init_db()
    FakeClient.responses = [FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})]
    monkeypatch.setattr("app.provider.httpx.AsyncClient", FakeClient)

    asyncio.run(completion([{"role": "user", "content": "use sk-test_DO_NOT_USE_000000000000"}], "provider-secret"))

    assert "sk-test_DO_NOT_USE_000000000000" not in FakeClient.last_json["messages"][0]["content"]
    event = rows("SELECT * FROM data_flow_events ORDER BY id DESC LIMIT 1")[0]
    assert event["sink"].startswith("model_api:")
    assert event["classification"] == "credential"
    assert event["redactions"] == 1


def test_completion_blocks_cloud_metadata_endpoint(monkeypatch) -> None:
    init_db()
    FakeClient.responses = [FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "unsafe"}}]})]
    monkeypatch.setattr("app.provider.httpx.AsyncClient", FakeClient)

    with pytest.raises(ProviderError) as exc:
        asyncio.run(completion([{"role": "user", "content": "test"}], "secret", base_url="http://169.254.169.254"))

    assert exc.value.error_type == "network_policy"
    assert FakeClient.responses


def test_completion_streams_provider_deltas(monkeypatch) -> None:
    init_db()
    deltas: list[str] = []
    monkeypatch.setattr("app.provider.httpx.AsyncClient", StreamingClient)

    result = asyncio.run(
        completion(
            [{"role": "user", "content": "stream"}],
            "secret",
            task_id=uuid.uuid4().hex,
            event_callback=lambda event, data: deltas.append(str(data["delta"])) if event == "model.delta" else None,
        )
    )

    assert result["content"] == "逐字"
    assert result["reasoning_content"] == "think"
    assert deltas == ["逐字"]
    assert result["_metrics"]["usage"]["total_tokens"] == 4
    assert StreamingClient.last_json["stream"] is True
