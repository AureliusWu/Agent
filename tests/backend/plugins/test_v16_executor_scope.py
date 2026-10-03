"""Plugin activation must retain the v16 Executor and recovery boundaries."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.executor import ExecutorToolCall, LocalWindowsExecutor
from app.runtime.recovery import MUTATION_TOOLS, SIDE_EFFECT_TOOLS
from app.runtime.tool_loop import prefetch_reads
from app.tools.outcomes import RuntimeToolOutcome


def executor_call(tmp_path: Path, **overrides) -> ExecutorToolCall:
    return replace(
        ExecutorToolCall(
            workspace=str(tmp_path),
            mode="full",
            name="read_file",
            arguments={"path": "note.txt"},
            tool_call_id="scope-call",
            approved_actions=[],
            approval_scope="once",
            conversation_id=1,
            task_id="scope-task",
            mcp_routes={},
        ),
        **overrides,
    )


@pytest.mark.parametrize("mode", ["full", "readonly"])
def test_executor_scope_denial_has_v16_bound_receipt_and_trusted_provenance(
    tmp_path, monkeypatch, mode
):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Scope rejection must precede hooks and dispatch")

    monkeypatch.setattr("app.runtime.executor.run_hooks", unexpected)
    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", unexpected)
    call = executor_call(tmp_path, mode=mode, available_tool_names=())
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(call))

    assert outcome.result["error_code"] == "tool_scope_violation"
    assert outcome.result["authorization_status"] == "denied"
    assert outcome.result["success"] is False and outcome.confirmed is False
    assert outcome.result["plugin"]["id"] == "builtin.reading"
    assert outcome.receipt is not None
    assert outcome.receipt.receipt_version == 2
    assert outcome.receipt.receipt_id
    assert outcome.receipt.task_id == call.task_id
    assert outcome.receipt.tool_call_id == call.tool_call_id
    assert outcome.receipt.normalized_arguments == {"path": "note.txt"}
    assert outcome.receipt.permission_decision == "denied"
    assert outcome.receipt.risk_level == outcome.risk == "low"
    assert outcome.result["receipt"] == outcome.receipt.as_dict()


def test_executor_preserves_scope_and_v16_receipt_when_plugin_has_provenance(
    tmp_path, monkeypatch
):
    seen = []

    async def hooks(event):
        seen.append(event.point)
        return []

    async def execute(**kwargs):
        seen.append(kwargs)
        return RuntimeToolOutcome(
            {"success": True, "status": "ok", "content": "synthetic",
             "plugin": {"id": "untrusted-output-owner", "version": "untrusted"}},
            False, "low", "builtin",
        )

    monkeypatch.setattr("app.runtime.executor.run_hooks", hooks)
    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", execute)
    call = executor_call(tmp_path, available_tool_names=("read_file",))
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(call))

    assert seen[0] == "pre_tool" and seen[-1] == "post_tool"
    assert seen[1]["available_tool_names"] == ("read_file",)
    assert seen[1]["permission_fn"] is call.permission_fn
    assert outcome.result["plugin"]["id"] == "builtin.reading"
    assert outcome.result["plugin"]["version"] != "untrusted"
    assert outcome.receipt.task_id == call.task_id
    assert outcome.receipt.tool_call_id == call.tool_call_id
    assert outcome.receipt.risk_level == "low"
    assert outcome.receipt.permission_decision == "approved"
    assert outcome.receipt.operation_kind == "read"


def test_scope_denial_keeps_readonly_error_without_reaching_hooks_or_dispatch(
    tmp_path, monkeypatch
):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Readonly scope denial must not consume any approval")

    monkeypatch.setattr("app.runtime.executor.run_hooks", unexpected)
    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", unexpected)
    call = executor_call(
        tmp_path, mode="readonly", name="create_file",
        arguments={"path": "must-not-exist.txt", "content": "synthetic"},
        available_tool_names=("read_file",),
    )
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(call))
    assert outcome.result["error_code"] == "read_only_mode"
    assert outcome.result["scope_violation"] is True
    assert outcome.result["authorization_status"] == "denied"
    assert outcome.result["success"] is False and outcome.confirmed is False
    assert outcome.receipt.permission_decision == "evaluated"
    assert outcome.receipt.risk_level == outcome.risk == "medium"
    assert outcome.receipt.task_id == call.task_id
    assert outcome.receipt.tool_call_id == call.tool_call_id
    assert not (tmp_path / "must-not-exist.txt").exists()


@pytest.mark.parametrize("name", ["unknown_capability", "mcp__unvalidated__operation"])
@pytest.mark.parametrize("mode", ["full", "readonly"])
def test_scope_denial_keeps_unknown_or_unverified_external_risk_critical(
    tmp_path, monkeypatch, name, mode
):
    async def unexpected(*args, **kwargs):
        raise AssertionError("Unverified scoped tools must never reach hooks or dispatch")

    monkeypatch.setattr("app.runtime.executor.run_hooks", unexpected)
    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", unexpected)
    call = executor_call(tmp_path, mode=mode, name=name, arguments={}, available_tool_names=())
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(call))
    assert outcome.result["error_code"] == "tool_scope_violation"
    assert outcome.result["scope_violation"] is True
    assert outcome.result["authorization_status"] == "denied"
    assert outcome.result["success"] is False and outcome.confirmed is False
    assert outcome.receipt.permission_decision == "denied"
    assert outcome.receipt.risk_level == outcome.risk == "critical"
    assert outcome.receipt.task_id == call.task_id
    assert outcome.receipt.tool_call_id == call.tool_call_id
    assert "plugin" not in outcome.result


@pytest.mark.parametrize("risk", ["high", "critical"])
def test_executor_keeps_dynamic_route_risk_in_v16_receipt(tmp_path, monkeypatch, risk):
    async def hooks(event):
        return []

    async def execute(**kwargs):
        return RuntimeToolOutcome({"success": True, "status": "ok"}, True, risk, "mcp")

    monkeypatch.setattr("app.runtime.executor.run_hooks", hooks)
    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", execute)
    call = executor_call(
        tmp_path, name="mcp__synthetic__operation", arguments={"value": "synthetic"},
        available_tool_names=("mcp__synthetic__operation",),
    )
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(call))
    assert outcome.receipt.risk_level == risk
    assert outcome.receipt.normalized_arguments == {"value": "synthetic"}
    assert outcome.receipt.task_id == call.task_id
    assert outcome.receipt.tool_call_id == call.tool_call_id
    assert "plugin" not in outcome.result


def test_executor_capabilities_keep_artifacts_and_only_configured_plugins(monkeypatch):
    monkeypatch.setattr("app.config.settings.speech_enabled", False)
    capabilities = asyncio.run(LocalWindowsExecutor().capabilities())
    assert {"artifacts", "files", "snapshots", "plugins", "resume", "cancel"} <= set(
        capabilities.features
    )
    assert {"artifact.docx.create", "artifact.pptx.create", "file_batch", "discover_tools"} <= set(
        capabilities.tools
    )
    assert not {"transcribe_audio", "synthesize_speech"} & set(capabilities.tools)


def prefetch_fixture(calls, cache, execute, scope):
    class Scheduler:
        def __init__(self, task_id, **kwargs):
            assert task_id == "scope-task"

        async def execute(self, batch, invoke):
            return [
                SimpleNamespace(call_id=item["id"], result=await invoke(item))
                for item in batch
            ]

    return prefetch_reads(
        pending_calls=calls, mcp_routes={}, extension_routes={}, signatures=Counter(),
        max_duplicate_calls=3, max_file_chars=20, max_parallel=2,
        read_cache=cache, execute=execute, workspace="synthetic-workspace",
        task_id="scope-task", conversation_id=1, approved_actions=[], approval_scope="once",
        permission_mode=lambda name: "readonly", allow_local_mcp=False,
        search_credentials=None, repair_attempt=0, retry_scope=[], record_cache=lambda hit: None,
        select_batch=lambda *args: calls, scheduler_factory=Scheduler,
        timestamp=lambda: "synthetic-stamp", perf_counter=lambda: 1.0,
        available_tool_names=scope,
    )


@pytest.mark.parametrize("scope", [(), ("find_symbol",)])
def test_prefetch_cannot_resurrect_cached_read_outside_task_scope(scope):
    def unexpected(*args, **kwargs):
        raise AssertionError("Denied reads must not observe the cache")

    async def execute(**kwargs):
        raise AssertionError("Denied reads must not reach the Executor")

    cache = SimpleNamespace(get_context_reference=unexpected, observe=unexpected, set=unexpected)
    calls = [{"id": "read", "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'}}]
    assert asyncio.run(prefetch_fixture(calls, cache, execute, scope)) == {}


def test_prefetch_validates_entire_batch_before_any_cache_or_executor_action():
    def unexpected(*args, **kwargs):
        raise AssertionError("The complete batch must be scoped before any read")

    async def execute(**kwargs):
        raise AssertionError("A mixed unscoped batch must not execute")

    calls = [
        {"id": "allowed", "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'}},
        {"id": "denied", "function": {"name": "read_file_range", "arguments": '{"path":"note.txt"}'}},
    ]
    cache = SimpleNamespace(get_context_reference=unexpected, observe=unexpected, set=unexpected)
    assert asyncio.run(prefetch_fixture(calls, cache, execute, ("read_file",))) == {}


def test_prefetch_forwards_task_scope_and_retains_snippet_source_version_contract():
    seen = []
    cached = []

    async def execute(**kwargs):
        seen.append(kwargs)
        return RuntimeToolOutcome({"success": True, "status": "ok"}, False, "low", "builtin")

    calls = [{"id": "read", "function": {"name": "read_file", "arguments": '{"path":"note.txt","max_chars":999}'}}]
    cache = SimpleNamespace(
        get_context_reference=lambda *args: None,
        observe=lambda *args: "synthetic-source-version",
        set=lambda *args, **kwargs: cached.append(kwargs),
    )
    output = asyncio.run(prefetch_fixture(calls, cache, execute, ("read_file",)))
    assert output["read"]["source"] == "builtin"
    assert seen[0]["available_tool_names"] == ("read_file",)
    assert seen[0]["mode"] == "readonly"
    assert seen[0]["arguments"]["max_chars"] == 20
    assert cached == [{"observed_before": "synthetic-source-version"}]


def test_recovery_preserves_v16_mutations_and_new_audio_side_effect_boundaries():
    assert {
        "file_batch", "undo_file_batch", "artifact.docx.create", "artifact.pptx.edit",
        "artifact.pdf.merge", "synthesize_speech",
    } <= MUTATION_TOOLS
    assert {"transcribe_audio", "synthesize_speech", "run_command"} <= SIDE_EFFECT_TOOLS
    assert "discover_tools" not in SIDE_EFFECT_TOOLS


@pytest.mark.parametrize("previous_status", ["completed", "uncertain"])
def test_scoped_runner_never_recovers_old_result_or_relabels_uncertain_side_effect(
    tmp_path, monkeypatch, previous_status
):
    from app.database import connect, now_iso
    from app.runtime import runner
    from app.runtime.recovery import set_operation_status
    from app.schemas import ChatRequest

    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (conversation_id, "synthetic scoped recovery", str(tmp_path), "readonly", stamp, stamp),
        )
    prepare = runner.prepare_operation
    prior_result = {"success": True, "status": "ok", "content": "synthetic-prior-result"}
    operations = []

    def prepare_prior_operation(*args, **kwargs):
        operation = prepare(*args, **kwargs)
        assert operation["tool"] == "create_file"
        set_operation_status(operation["execution_id"], previous_status, prior_result)
        operations.append(operation["execution_id"])
        return {**operation, "created": False, "status": previous_status, "result": prior_result}

    def unexpected_recovery(*args, **kwargs):
        raise AssertionError("A capability outside task scope must never recover a prior result")

    requests = 0

    async def scripted(messages, api_key=None, **kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "prior-write", "type": "function",
                    "function": {
                        "name": "create_file",
                        "arguments": '{"path":"must-not-exist.txt","content":"synthetic"}',
                    },
                }],
            }
        assert "synthetic-prior-result" not in json.dumps(messages)
        return {"role": "assistant", "content": "工具已被当前只读任务阻止，没有修改文件。"}

    monkeypatch.setattr(runner, "prepare_operation", prepare_prior_operation)
    monkeypatch.setattr(runner, "recover_tool_operation", unexpected_recovery)
    monkeypatch.setattr(runner, "provider_ready", lambda api_key: False)
    result = asyncio.run(runner.run_chat(
        ChatRequest(
            conversation_id=conversation_id, task_id=task_id,
            content="介绍当前能力，不修改文件。", orchestration_mode="single",
        ),
        completion_fn=scripted,
    ))
    assert result["pending_actions"] == []
    assert not (tmp_path / "must-not-exist.txt").exists()
    assert len(operations) == 1
    with connect() as db:
        operation = db.execute(
            "SELECT status,result FROM task_operations WHERE execution_id=?", (operations[0],),
        ).fetchone()
        trace = db.execute(
            "SELECT output FROM tool_runs WHERE task_id=? AND tool='create_file'", (task_id,),
        ).fetchone()
    assert operation["status"] == previous_status
    assert json.loads(operation["result"]) == prior_result
    recorded = json.loads(trace["output"])
    assert recorded["error_code"] == "read_only_mode"
    assert recorded["scope_violation"] is True
    # The existing persisted-detail redactor treats every authorization-key
    # value as sensitive. Direct Executor tests above prove the original
    # decision; do not weaken that privacy boundary to persist this label.
    assert recorded["authorization_status"] == "***"
    assert recorded["success"] is False
    assert recorded["receipt"]["permission_decision"] == "evaluated"
    assert recorded["receipt"]["risk_level"] == "medium"
    assert recorded["receipt"]["task_id"] == task_id
    assert recorded["receipt"]["tool_call_id"] == "prior-write"
