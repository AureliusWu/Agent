from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.database import connect, now_iso, rows
from app.runtime.recovery import create_checkpoint, list_checkpoints, load_checkpoint, operation_status, prepare_operation
from app.runtime.runner import run_chat
from app.schemas import ChatRequest


def _request(workspace: str, prompt: str, *, mode: str = "full") -> ChatRequest:
    stamp = now_iso()
    with connect() as db:
        conversation_id = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("round admission", workspace, mode, stamp, stamp),
        ).lastrowid
    return ChatRequest(conversation_id=conversation_id, task_id=uuid.uuid4().hex, content=prompt, orchestration_mode="single")


def _call(call_id: str, name: str, **arguments) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _assistant(*calls: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def _result(messages: list[dict], call_id: str) -> dict:
    return json.loads(next(message["content"] for message in messages if message.get("tool_call_id") == call_id))


def test_discovery_only_changes_next_round_admission(tmp_path, monkeypatch):
    """SIYI-1602-ADMISSION: exact model request list, immutable within the batch."""
    monkeypatch.setattr("app.runtime.runner.provider_ready", lambda *_: False)
    (tmp_path / "sample.py").write_text("def round_fixture_symbol():\n    return 1\n", encoding="utf-8")
    request = _request(str(tmp_path), "介绍能力")
    rounds = 0

    async def complete(messages, api_key=None, tools=None, **kwargs):
        nonlocal rounds
        rounds += 1
        names = {tool["function"]["name"] for tool in tools or []}
        if rounds == 1:
            assert "discover_tools" in names and "find_symbol" not in names
            return _assistant(
                _call("discovery", "discover_tools", query="find_symbol"),
                _call("same-round", "find_symbol", query="round_fixture_symbol"),
                _call("unadvertised-write", "write_file", path="must-not-exist.txt", content="blocked"),
            )
        if rounds == 2:
            assert "find_symbol" in names
            assert _result(messages, "discovery")["activated_tools"] == ["find_symbol"]
            assert _result(messages, "same-round")["error_code"] == "tool_not_admitted"
            assert _result(messages, "unadvertised-write")["error_code"] == "tool_not_admitted"
            return _assistant(_call("next-round", "find_symbol", query="round_fixture_symbol"))
        assert _result(messages, "next-round")["success"] is True
        return {"role": "assistant", "content": "工具已在下一轮执行。"}

    result = asyncio.run(run_chat(request, completion_fn=complete))
    assert result["task_status"] == "completed"
    assert rounds == 3
    assert not (tmp_path / "must-not-exist.txt").exists()
    assert rows("SELECT 1 FROM task_operations WHERE task_id=?", (request.task_id,)) == []
    persisted = [
        load_checkpoint(request.task_id, item["sequence"])["state"]
        for item in list_checkpoints(request.task_id)
    ]
    first_batch = next(
        state for state in persisted
        if any(call["id"] == "same-round" for call in state["pending_tool_calls"])
    )
    assert "find_symbol" in first_batch["selected_tool_names"]
    assert "find_symbol" not in first_batch["pending_round_tool_names"]


def test_no_workspace_model_cannot_send_unadvertised_network_call(monkeypatch):
    """SIYI-1602-ADMISSION: no-workspace network path fails closed too."""
    request = _request("", "请简短问候")
    rounds = 0

    async def unexpected(*args, **kwargs):
        pytest.fail("An unadvertised web tool reached execution or hooks")

    monkeypatch.setattr("app.runtime.executor.run_hooks", unexpected)
    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", unexpected)

    async def complete(messages, api_key=None, tools=None, **kwargs):
        nonlocal rounds
        rounds += 1
        assert not tools
        if rounds == 1:
            return _assistant(_call("network", "web_fetch", url="https://example.invalid/"))
        assert _result(messages, "network")["error_code"] == "tool_not_admitted"
        return {"role": "assistant", "content": "你好。"}

    assert asyncio.run(run_chat(request, completion_fn=complete))["task_status"] == "completed"
    assert rounds == 2


@pytest.mark.parametrize("snapshot", [None, [], ["read_file"]])
def test_recovery_does_not_replay_unadmitted_uncertain_operation(tmp_path, monkeypatch, snapshot):
    """SIYI-1602-ADMISSION: preserve uncertain work even with explicit retry intent."""
    request = _request(str(tmp_path), "写入 test.txt")
    stamp = now_iso()
    call = _call("prior", "write_file", path="test.txt", content="do not replay")
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at,resumable) VALUES(?,?,?,?,?,?,?)",
            (request.task_id, request.conversation_id, "interrupted", request.content, stamp, stamp, 1),
        )
    checkpoint = create_checkpoint(request.task_id, str(tmp_path), "implementation", "before_side_effect", {
        "goal": request.content,
        "pending_tool_calls": [call],
        "selected_tool_names": ["write_file"],
        "pending_round_tool_names": snapshot,
        "executor_messages": [{"role": "user", "content": request.content}, _assistant(call)],
    })
    prior = prepare_operation(request.task_id, checkpoint["sequence"], call, {"path": "test.txt", "content": "do not replay"}, side_effect=True)

    async def unexpected(*args, **kwargs):
        pytest.fail("Recovery must stop before any fresh tool execution or model round")

    monkeypatch.setattr("app.runtime.runner.provider_ready", lambda *_: False)
    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", unexpected)
    resumed = request.model_copy(update={"resume": True, "retry_uncertain": True})
    result = asyncio.run(run_chat(resumed, completion_fn=unexpected))
    assert result["task_status"] == "interrupted"
    assert result["recovery"]["retry_requires_confirmation"] is False
    assert not (tmp_path / "test.txt").exists()
    preserved = operation_status(request.task_id, prior["execution_id"])
    assert preserved["status"] in {"running", "uncertain"}
    assert preserved["result"] is None


@pytest.mark.parametrize("snapshot", [None, ["discover_tools"]])
def test_resume_does_not_treat_next_round_selection_as_authority_for_pending_batch(tmp_path, monkeypatch, snapshot):
    request = _request(str(tmp_path), "介绍能力")
    stamp = now_iso()
    call = _call("pending", "write_file", path="must-not-exist.txt", content="blocked")
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at,resumable) VALUES(?,?,?,?,?,?,?)",
            (request.task_id, request.conversation_id, "interrupted", request.content, stamp, stamp, 1),
        )
    create_checkpoint(request.task_id, str(tmp_path), "implementation", "before_side_effect", {
        "goal": request.content,
        "pending_tool_calls": [call],
        "selected_tool_names": ["discover_tools", "write_file"],
        "pending_round_tool_names": snapshot,
        "executor_messages": [{"role": "user", "content": request.content}, _assistant(call)],
    })
    monkeypatch.setattr("app.runtime.runner.provider_ready", lambda *_: False)

    async def complete(messages, api_key=None, tools=None, **kwargs):
        assert "write_file" in {tool["function"]["name"] for tool in tools or []}
        assert _result(messages, "pending")["error_code"] == "tool_not_admitted"
        return {"role": "assistant", "content": "旧工具提议已阻止。"}

    result = asyncio.run(run_chat(request.model_copy(update={"resume": True}), completion_fn=complete))
    assert result["task_status"] == "completed"
    assert not (tmp_path / "must-not-exist.txt").exists()
    assert rows("SELECT 1 FROM task_operations WHERE task_id=?", (request.task_id,)) == []
