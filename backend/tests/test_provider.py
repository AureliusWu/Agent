import asyncio
import uuid

import pytest

from app.database import init_db, rows
from app.provider import ProviderError, completion, provider_health


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

    def __init__(self, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return self.responses.pop(0)


def test_completion_retries_and_persists_usage(monkeypatch) -> None:
    init_db()
    task_id = uuid.uuid4().hex
    FakeClient.responses = [
        FakeResponse(429, {}),
        FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": "完成"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}),
    ]
    monkeypatch.setattr("app.provider.httpx.AsyncClient", FakeClient)

    async def no_sleep(_):
        return None

    monkeypatch.setattr("app.provider.asyncio.sleep", no_sleep)

    result = asyncio.run(completion([{"role": "user", "content": "test"}], "secret", task_id=task_id))
    recorded = rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))[0]

    assert result["content"] == "完成"
    assert result["_metrics"]["retry_count"] == 1
    assert recorded["total_tokens"] == 10
    assert recorded["retry_count"] == 1
    assert recorded["success"] == 1


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
