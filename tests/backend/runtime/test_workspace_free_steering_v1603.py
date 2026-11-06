import asyncio
import uuid
from dataclasses import replace

import pytest
from fastapi import HTTPException

from app.api.routes.chat import steer_running_task
from app.cognition.reasoning_summary import PRIVATE_REASONING_KEY
from app.database import connect, now_iso, rows
from app.providers.provider import ProviderError
from app.runtime import orchestration
from app.runtime.queue_service import cancel, consume_steering_at_safe_point, get_item
from app.runtime.recovery import SteeringCheckpointConflict, create_checkpoint, load_checkpoint
from app.runtime.runner import run_chat
from app.schemas import ChatRequest, SteeringRequest


GUIDANCE = "Cancel the proposed fetch and explain the offline alternative."
CANCELLED_GUIDANCE = "This guidance was cancelled while checkpointing."
REQUEST = "Find the latest information online."


def conversation() -> int:
    stamp = now_iso()
    with connect() as db:
        return db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("Workspace-free steering test", "", "ask", stamp, stamp),
        ).lastrowid


@pytest.fixture(autouse=True)
def no_remote_effects(monkeypatch):
    async def reject_network(*args, **kwargs):
        pytest.fail("A superseded web call reached the executor")

    monkeypatch.setattr("app.kernel.adapters.RuntimeToolProvider.execute", reject_network)
    monkeypatch.setattr("app.runtime.runner.schedule_title_generation", lambda *args: None)


def test_steering_rolls_back_on_cancellation_and_resumes_exactly_once(monkeypatch):
    conversation_id, task_id = conversation(), uuid.uuid4().hex
    request = ChatRequest(conversation_id=conversation_id, task_id=task_id, content=REQUEST)
    calls, events = [], []
    state = {}

    async def complete(messages, api_key=None, **kwargs):
        calls.append(([dict(item) for item in messages], dict(kwargs)))
        if len(calls) == 1:
            assert "web_fetch" in {tool["function"]["name"] for tool in kwargs["tools"]}
            state["old"] = create_checkpoint(task_id, "", "conversation", "before_first_response", {})
            state["keep"] = (await steer_running_task(task_id, SteeringRequest(content=GUIDANCE)))["id"]
            state["drop"] = (await steer_running_task(task_id, SteeringRequest(content=CANCELLED_GUIDANCE)))["id"]
            return {
                "role": "assistant",
                "content": "obsolete response",
                PRIVATE_REASONING_KEY: "private obsolete response reasoning",
                "tool_calls": [{
                    "id": "obsolete-fetch", "type": "function",
                    "function": {"name": "web_fetch", "arguments": '{"url":"https://example.invalid"}'},
                }],
                "_metrics": {"usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}},
            }
        assert get_item(state["keep"]).status == "consumed"
        if len(calls) == 2:
            raise ProviderError("simulated provider interruption", "network_error")
        return {"role": "assistant", "content": "Use the available offline data."}

    def checkpoint(*args, **kwargs):
        if kwargs.get("steering_item_ids") == (state.get("keep"), state.get("drop")):
            # Inject a cancellation after the safe-point read, before its transaction.
            cancel(state["drop"])
            with pytest.raises(SteeringCheckpointConflict):
                create_checkpoint(*args, **kwargs)
            assert load_checkpoint(task_id)["sequence"] == state["old"]["sequence"]
            assert get_item(state["keep"]).status == "pending"
            state["rolled_back"] = True
            # The orchestrator must re-read the queue, never use the cancelled copy.
            raise SteeringCheckpointConflict("injected concurrent cancellation")
        return create_checkpoint(*args, **kwargs)

    monkeypatch.setattr(orchestration, "create_checkpoint", checkpoint)
    def record(kind, payload):
        events.append((kind, payload))

    async def scenario():
        waiting = await run_chat(request, completion_fn=complete, event_callback=record)
        assert waiting["task_status"] == "waiting_provider"
        assert state["rolled_back"]
        assert get_item(state["drop"]).status == "cancelled"
        saved = load_checkpoint(task_id)
        assert saved["reason"] == "steering_applied"
        assert saved["state"]["steering_item_ids"] == [state["keep"]]
        assert not saved["state"].get("pending_tool_calls")
        assert GUIDANCE in str(saved["state"]["workspace_free_steering"])
        assert "obsolete" not in str(saved["state"])
        assert CANCELLED_GUIDANCE not in str(saved["state"])

        with pytest.raises(HTTPException) as rejected:
            await run_chat(request.model_copy(update={"resume": True, "checkpoint_sequence": state["old"]["sequence"]}), completion_fn=complete)
        assert rejected.value.detail["code"] == "checkpoint_superseded_by_steering"

        return await run_chat(request.model_copy(update={"resume": True}), completion_fn=complete, event_callback=record), saved

    completed, saved = asyncio.run(scenario())
    assert completed["task_status"] == "completed"
    assert len(calls) == 3
    for messages, kwargs in calls[1:]:
        assert sum(GUIDANCE in item.get("content", "") for item in messages) == 1
        assert not any(CANCELLED_GUIDANCE in item.get("content", "") for item in messages)
        assert "obsolete" not in str(messages)
        assert kwargs["estimated_input_tokens"] > calls[0][1]["estimated_input_tokens"]
    consumed = [payload for kind, payload in events if kind == "queue.consumed"]
    assert consumed == [{
        "operation": "steer", "item_ids": [state["keep"]], "safe_point": "after_model",
        "checkpoint_sequence": saved["sequence"],
    }]
    task = rows("SELECT model_calls, tool_calls, total_tokens FROM agent_tasks WHERE id=?", (task_id,))[0]
    assert task == {"model_calls": 2, "tool_calls": 0, "total_tokens": 52}


def test_guidance_rechecks_context_before_second_provider_call(monkeypatch):
    conversation_id, task_id = conversation(), uuid.uuid4().hex
    request_budget = orchestration.request_budget
    calls = []

    def too_big_after_guidance(messages, *args, **kwargs):
        result = request_budget(messages, *args, **kwargs)
        if any(GUIDANCE in item.get("content", "") for item in messages):
            return replace(result, exceeds_context_window=True)
        return result

    async def complete(messages, api_key=None, **kwargs):
        calls.append(messages)
        assert len(calls) == 1
        await steer_running_task(task_id, SteeringRequest(content=GUIDANCE))
        return {"role": "assistant", "content": "old draft"}

    monkeypatch.setattr(orchestration, "request_budget", too_big_after_guidance)
    result = asyncio.run(run_chat(ChatRequest(conversation_id=conversation_id, task_id=task_id, content=REQUEST), completion_fn=complete))
    assert result["task_status"] == "waiting_provider"
    assert len(calls) == 1
    latest = load_checkpoint(task_id)
    assert latest["reason"] == "context_window_exceeded"
    assert GUIDANCE in str(latest["state"]["workspace_free_steering"])


def test_resuming_pre_upgrade_checkpoint_preserves_already_consumed_guidance():
    conversation_id, task_id = conversation(), uuid.uuid4().hex
    request = ChatRequest(conversation_id=conversation_id, task_id=task_id, content=REQUEST)
    calls = []

    async def complete(messages, api_key=None, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            create_checkpoint(task_id, "", "conversation", "after_tool_call", {})
            await steer_running_task(task_id, SteeringRequest(content=GUIDANCE))
            assert consume_steering_at_safe_point(task_id)[0].content == GUIDANCE
            raise ProviderError("simulated pre-upgrade interruption", "network_error")
        return {"role": "assistant", "content": "Offline only."}

    async def scenario():
        result = await run_chat(request, completion_fn=complete)
        assert result["task_status"] == "waiting_provider"
        return await run_chat(request.model_copy(update={"resume": True}), completion_fn=complete)

    assert asyncio.run(scenario())["task_status"] == "completed"
    assert [item["content"] for item in calls[1] if item["role"] == "user"] == [
        REQUEST, f"[管理员运行中引导]\n{GUIDANCE}",
    ]
