"""Explicit chat reasoning choices reach the workspace-free model boundary."""
import asyncio
import uuid

import pytest

from app.config import settings
from app.database import connect, init_db, now_iso, rows
from app.runtime.runner import run_chat
from app.schemas import ChatRequest


PREFERRED_MODEL = "explicit-chat-reasoning-model"
SYNTHETIC_KEY = "synthetic-chat-reasoning-test-key"


@pytest.fixture(autouse=True)
def isolated_chat(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "explicit-reasoning.db")
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "unused-provider-settings.json"))
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "false")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "tavily_api_key", "")
    monkeypatch.setattr(settings, "brave_api_key", "")
    monkeypatch.setattr(settings, "model_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(settings, "model_name", "different-default-model")
    monkeypatch.setattr(settings, "model_light_name", "different-light-model")
    monkeypatch.setattr(settings, "model_medium_name", "different-medium-model")
    monkeypatch.setattr(settings, "model_strong_name", "different-strong-model")
    monkeypatch.setattr(settings, "model_data_routing_enabled", False)
    monkeypatch.setattr("app.runtime.runner.schedule_title_generation", lambda *_args, **_kwargs: None)

    async def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("Only the supplied chat mock may be called")

    monkeypatch.setattr("app.runtime.runner.completion", forbidden_provider)
    monkeypatch.setattr("app.providers.provider.completion", forbidden_provider)
    init_db()


def _conversation():
    conversation_id = uuid.uuid4().int % 1_000_000_000
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "synthetic reasoning", "", "ask", stamp, stamp),
        )
    return conversation_id


def _reply(content):
    return {"role": "assistant", "content": content,
            "_metrics": {"usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}}


@pytest.mark.parametrize("effort,expected", [
    ("auto", None), ("low", "low"), ("medium", "medium"), ("high", "high"),
])
def test_workspace_free_chat_passes_explicit_reasoning_and_preserves_model(effort, expected):
    conversation_id, task_id = _conversation(), uuid.uuid4().hex
    calls = []

    async def chat_completion(messages, api_key=None, **kwargs):
        assert api_key == SYNTHETIC_KEY
        assert messages
        calls.append(dict(kwargs))
        return _reply("你好，可以直接聊天。")

    result = asyncio.run(run_chat(
        ChatRequest(conversation_id=conversation_id, content="你好", task_id=task_id,
                    preferred_model=PREFERRED_MODEL, reasoning_effort=effort),
        SYNTHETIC_KEY, completion_fn=chat_completion,
    ))

    assert result["task_status"] == "completed"
    assert len(calls) == 1
    assert calls[0]["phase"] == "conversation"
    assert calls[0]["tools"] is None
    assert calls[0]["model"] == PREFERRED_MODEL
    assert "reasoning_effort" in calls[0]
    assert calls[0]["reasoning_effort"] == expected
    task = rows("SELECT status,model_route FROM agent_tasks WHERE id=?", (task_id,))[0]
    assert task["status"] == "completed"
    assert PREFERRED_MODEL in task["model_route"]


def test_identity_repair_reuses_explicit_chat_reasoning_and_model():
    conversation_id, task_id = _conversation(), uuid.uuid4().hex
    calls = []

    async def chat_completion(messages, api_key=None, **kwargs):
        assert api_key == SYNTHETIC_KEY
        calls.append((list(messages), dict(kwargs)))
        return _reply("我是 ChatGPT。" if len(calls) == 1 else "我是夏目心，可以继续聊天。")

    result = asyncio.run(run_chat(
        ChatRequest(conversation_id=conversation_id, content="你好", task_id=task_id,
                    preferred_model=PREFERRED_MODEL, reasoning_effort="high"),
        SYNTHETIC_KEY, completion_fn=chat_completion,
    ))

    assert result["task_status"] == "completed"
    assert "夏目心" in result["content"]
    assert len(calls) == 2
    assert "身份漂移" in calls[1][0][-1]["content"]
    for _messages, kwargs in calls:
        assert kwargs["phase"] == "conversation"
        assert kwargs["tools"] is None
        assert kwargs["model"] == PREFERRED_MODEL
        assert kwargs["reasoning_effort"] == "high"
    assert calls[0][1] == calls[1][1]
