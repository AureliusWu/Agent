"""STUDY-OUTPUT-01: invalid output cannot authorize success or replay."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace

import pytest

from app.database import connect, now_iso, rows
from app.providers.schema_validation import StructuredOutputError, parse_output
from app.runtime.executor import ExecutorToolCall, LocalWindowsExecutor
from app.runtime.runner import run_chat
from app.schemas import ChatRequest
from app.tools.outcomes import RuntimeToolOutcome
from app.tools.output_contract import (
    MAX_TOOL_OUTPUT_BYTES, ToolOutputContractError, envelope_schema,
    prepare_tool_output_schema, validate_tool_output,
)
from app.tools.registry import REGISTRY
from app.tools.scheduler import ToolScheduler


def call_for(tmp_path, **changes) -> ExecutorToolCall:
    return replace(ExecutorToolCall(
        workspace=str(tmp_path), mode="full", name="read_file", arguments={"path": "note.txt"},
        tool_call_id="contract-call", approved_actions=[], approval_scope="once",
        conversation_id=1, task_id="contract-task", mcp_routes={},
    ), **changes)


@pytest.mark.parametrize("result", [
    {}, {"success": "yes", "status": "ok"}, {"success": True},
    {"success": True, "status": "error"}, {"success": False, "status": "ok"},
    {"success": True, "status": "ok", "error_code": "secret-invalid"},
    {"success": True, "status": "ok", "data": []},
    {"success": True, "status": "ok", "data": {"exit_code": "secret-invalid"}},
    {"success": True, "status": "ok", "data": {"exit_code": True}},
    {"success": True, "status": "ok", "metadata": {"duration_ms": -1}},
    {"success": True, "status": "ok", "data": {"value": float("nan")}},
    {"success": True, "status": "ok", "data": {"value": object()}},
    {"success": True, "status": "ok", "data": {1: "invalid key"}},
    {"success": True, "status": "ok", "content": "x" * MAX_TOOL_OUTPUT_BYTES},
])
def test_malformed_output_is_fixed_sensitive_free_failure(result) -> None:
    with pytest.raises(ToolOutputContractError) as error:
        validate_tool_output(result, prepare_tool_output_schema(envelope_schema()))
    assert error.value.dispatch_started is True
    assert "secret-invalid" not in str(error.value)


def test_cyclic_output_is_bounded() -> None:
    result = {"success": True, "status": "ok"}
    result["cycle"] = result
    with pytest.raises(ToolOutputContractError):
        validate_tool_output(result, envelope_schema())


@pytest.mark.parametrize("data", [None, [], "invalid"])
@pytest.mark.parametrize("schema", [{}, {"type": "object", "properties": {"data": {}}}])
def test_custom_schema_cannot_weaken_host_envelope(data, schema) -> None:
    with pytest.raises(ToolOutputContractError):
        validate_tool_output({"success": True, "status": "ok", "data": data}, schema)


def test_custom_schema_cannot_accept_truthy_success_string() -> None:
    with pytest.raises(ToolOutputContractError):
        validate_tool_output({"success": "yes", "status": "ok"}, {})


@pytest.mark.parametrize("result", [
    {"success": True, "status": "ok", "data": {"exit_code": 7}},
    {"success": True, "status": "ok", "exit_code": 7, "data": {"exit_code": 0}},
    {"success": True, "status": "ok", "exit_code": True, "data": {"exit_code": 0}},
])
def test_command_exit_failure_cannot_claim_success(result) -> None:
    with pytest.raises(ToolOutputContractError):
        validate_tool_output(result, envelope_schema(), tool_name="run_command")


@pytest.mark.parametrize("result", [
    {"success": False, "status": "confirmation_required", "approval_key": "test-only"},
    {"success": False, "status": "error", "error_code": "tool_timeout"},
    {"success": True, "status": "ok", "dry_run": True, "data": {"bytes_after": 10}},
])
def test_legacy_denial_timeout_and_dry_run_envelopes_remain_valid(result) -> None:
    assert validate_tool_output(result, envelope_schema()) == result


def test_largest_unicode_read_does_not_weaken_provider_limit() -> None:
    text = "汉" * 200_000
    result = {"success": True, "status": "ok", "content": text, "data": {"content": text}}
    assert validate_tool_output(result, envelope_schema()) == result
    with pytest.raises(StructuredOutputError):
        parse_output(result)


def test_executor_snapshots_output_and_replaces_untrusted_receipt(tmp_path, monkeypatch) -> None:
    source = {"success": True, "status": "ok", "data": {"content": "original"}, "receipt": {"success": "forged"}}

    async def execute(**kwargs):
        return RuntimeToolOutcome(source, False, "low", "builtin")

    async def hooks(event):
        if event.point == "post_tool":
            source["data"]["content"] = "late mutation"
            event.payload["receipt"]["success"] = "observer mutation"
        return []

    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", execute)
    monkeypatch.setattr("app.runtime.executor.run_hooks", hooks)
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(call_for(tmp_path)))
    assert outcome.result["data"]["content"] == "original"
    assert outcome.result["receipt"] == outcome.receipt.as_dict()
    assert outcome.receipt.success is True


def test_bad_schema_fails_before_hooks_and_dispatch(tmp_path, monkeypatch) -> None:
    spec = replace(REGISTRY["read_file"], output_schema={"$ref": "https://invalid.invalid/schema"})
    monkeypatch.setitem(REGISTRY, "read_file", spec)

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid schema must precede hook/dispatch")

    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", forbidden)
    monkeypatch.setattr("app.runtime.executor.run_hooks", forbidden)
    with pytest.raises(ToolOutputContractError) as error:
        asyncio.run(LocalWindowsExecutor().execute_tool(call_for(tmp_path)))
    assert error.value.dispatch_started is False


def test_schema_is_snapshotted_before_hook_await(tmp_path, monkeypatch) -> None:
    schema = envelope_schema()
    monkeypatch.setitem(REGISTRY, "read_file", replace(REGISTRY["read_file"], output_schema=schema))

    async def hooks(event):
        schema.clear()
        return []

    async def execute(**kwargs):
        return RuntimeToolOutcome({"success": "wrong", "status": "ok"}, False, "low", "builtin")

    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", execute)
    monkeypatch.setattr("app.runtime.executor.run_hooks", hooks)
    with pytest.raises(ToolOutputContractError):
        asyncio.run(LocalWindowsExecutor().execute_tool(call_for(tmp_path)))


def test_scheduler_drains_parallel_reads_on_contract_failure(monkeypatch) -> None:
    monkeypatch.setattr("app.tools.scheduler.emit_task_event", lambda *args, **kwargs: None)
    stopped = []

    async def scenario():
        ready = asyncio.Event()

        async def invoke(call):
            if call["id"] == "broken":
                await ready.wait()
                raise ToolOutputContractError("invalid_json", dispatch_started=True)
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)

        with pytest.raises(ToolOutputContractError):
            await ToolScheduler("contract-task").execute([
                {"id": name, "function": {"name": "read_file"}}
                for name in ("broken", "waiting")
            ], invoke)
        assert stopped == [True]

    asyncio.run(scenario())


def test_written_side_effect_with_invalid_output_stops_and_is_not_replayed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.runtime.runner.provider_ready", lambda *args: False)
    stamp = now_iso()
    with connect() as db:
        conversation = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("contract isolation", str(tmp_path), "full", stamp, stamp),
        ).lastrowid
    request = ChatRequest(conversation_id=conversation, task_id=uuid.uuid4().hex,
                          content="Create note.txt with the text once.", orchestration_mode="single")
    calls = {"model": 0, "execute": 0}

    async def model(messages, api_key=None, **kwargs):
        calls["model"] += 1
        assert calls["model"] == 1, "contract failure must not reach another model round"
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": "create-once", "type": "function", "function": {
                "name": "create_file", "arguments": json.dumps({"path": "note.txt", "content": "once"}),
            },
        }]}

    async def execute(**kwargs):
        calls["execute"] += 1
        # Simulate a side effect whose body returned without a valid journal or
        # receipt. Only this test-owned path is written.
        (tmp_path / "note.txt").write_text("once", encoding="utf-8")
        return RuntimeToolOutcome({"success": "malformed", "status": "ok"}, True, "medium", "builtin")

    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", execute)
    first = asyncio.run(run_chat(request, completion_fn=model))
    assert first["task_status"] == "interrupted"
    task = rows("SELECT current_step FROM agent_tasks WHERE id=?", (request.task_id,))[0]
    assert task["current_step"] == "tool_output_contract"
    operations = rows("SELECT status FROM task_operations WHERE task_id=?", (request.task_id,))
    assert operations == [{"status": "uncertain"}]
    assert not rows("SELECT 1 FROM tool_receipts WHERE task_id=?", (request.task_id,))
    resumed = asyncio.run(run_chat(request.model_copy(update={"resume": True, "allow_workspace_drift": True}), completion_fn=model))
    assert resumed["task_status"] == "interrupted"
    assert calls == {"model": 1, "execute": 1}
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "once"


def test_parallel_read_contract_failure_stops_runner_without_creating_operation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.runtime.runner.provider_ready", lambda *args: False)
    monkeypatch.setattr("app.config.settings.max_parallel_tool_calls", 2)
    stamp = now_iso()
    with connect() as db:
        conversation = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("parallel contract isolation", str(tmp_path), "full", stamp, stamp),
        ).lastrowid
    request = ChatRequest(conversation_id=conversation, task_id=uuid.uuid4().hex,
                          content="Read a.txt and b.txt.", orchestration_mode="single")

    async def scenario():
        started = asyncio.Event()
        stopped = []
        rounds = 0

        async def model(messages, api_key=None, **kwargs):
            nonlocal rounds
            rounds += 1
            assert rounds == 1
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": path, "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path": path}),
                }} for path in ("a.txt", "b.txt")
            ]}

        async def execute(**kwargs):
            if kwargs["arguments"]["path"] == "a.txt":
                await started.wait()
                return RuntimeToolOutcome({"success": "malformed", "status": "ok"}, False, "low", "builtin")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)

        monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", execute)
        result = await asyncio.wait_for(run_chat(request, completion_fn=model), 5)
        assert result["task_status"] == "interrupted"
        assert rounds == 1 and stopped == [True]

    asyncio.run(scenario())
    assert not rows("SELECT 1 FROM task_operations WHERE task_id=?", (request.task_id,))
