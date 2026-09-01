import asyncio

from app.runtime import runner
from app.runtime.finalization import FinalizationCallbacks
from app.schemas import ChatRequest


def test_workspace_free_runner_boundary_forwards_runtime_seams(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "unused"}

    async def orchestrate(payload, api_key, **kwargs):
        captured["payload"] = payload
        captured["api_key"] = api_key
        captured.update(kwargs)
        return {"task_status": "completed", "content": "delegated"}

    monkeypatch.setattr("app.runtime.orchestration.run_workspace_free_conversation", orchestrate)
    payload = ChatRequest(conversation_id=7, content="你好")
    services = object()
    runtime_limits = object()
    budget_contract = object()
    agent_profile = object()
    event_callback = object()
    search_credentials = {"tavily": "binding"}

    result = asyncio.run(
        runner._run_workspace_free_conversation(
            payload,
            "request-credential",
            task_id="task-7",
            services=services,
            complete=completion,
            runtime_limits=runtime_limits,
            budget_contract=budget_contract,
            agent_profile=agent_profile,
            event_callback=event_callback,
            search_credentials=search_credentials,
        )
    )

    finalization_callbacks = captured.pop("finalization_callbacks")
    assert result == {"task_status": "completed", "content": "delegated"}
    assert isinstance(finalization_callbacks, FinalizationCallbacks)
    assert finalization_callbacks.extract_candidates is runner.extract_explicit_candidates
    assert finalization_callbacks.record_interaction is runner.record_completed_interaction
    assert finalization_callbacks.consolidate is runner.maybe_consolidate_idle
    assert finalization_callbacks.schedule_title is runner.schedule_title_generation
    assert finalization_callbacks.run_hooks is runner.run_hooks
    assert captured == {
        "payload": payload,
        "api_key": "request-credential",
        "task_id": "task-7",
        "services": services,
        "complete": completion,
        "runtime_limits": runtime_limits,
        "budget_contract": budget_contract,
        "agent_profile": agent_profile,
        "event_callback": event_callback,
        "search_credentials": search_credentials,
    }
