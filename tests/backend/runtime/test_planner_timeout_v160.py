"""Ready-provider planner timeouts use the same bounded Runtime recovery contract.

All model waits are synthetic; transport/tool guards prohibit external execution.
The 0.2-second segment and three-consecutive-failure limits match the fixed Eval.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import settings
from app.context.budget import RequestBudget
from app.database import connect, init_db, now_iso
from app.providers.configuration import ProviderConfiguration
from app.providers.costs import LEDGER_KEY, task_cost_fields
from app.providers.provider import ProviderError
from app.runtime import runner
from app.runtime.cost_budget import COST_ERRORS, CostBudgetBlocked, mark_uncertain, reserve
from app.runtime.task_state import TaskStatus
from app.schemas import ChatRequest


@pytest.fixture
def ready_planner(tmp_path: Path, monkeypatch) -> tuple[int, str]:
    """Reuse the ready-mock admission boundary used by test_task_runner.py."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "planner-timeout.db")
    monkeypatch.setattr(settings, "log_path", tmp_path / "planner-timeout.log")
    monkeypatch.setattr(settings, "extension_directory", tmp_path / "extensions")
    for name in ("deepseek_api_key", "tavily_api_key", "brave_api_key"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "speech_enabled", False)
    monkeypatch.setattr(settings, "allow_local_mcp", False)
    monkeypatch.setattr(settings, "multi_agent_enabled", False)
    monkeypatch.setattr(settings, "model_data_routing_enabled", False)
    monkeypatch.setenv("AGENT_DESKTOP_DATA_DIRECTORY", str(tmp_path / "desktop"))
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "unused-provider.json"))
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "mock")
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "false")

    def forbidden_network(*args, **kwargs):
        pytest.fail("planner regression tests must not create a network client")

    async def forbidden_tool(*args, **kwargs):
        pytest.fail("planner regression tests must not execute a tool")

    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", forbidden_network)
    monkeypatch.setattr("app.kernel.adapters.RuntimeToolProvider.execute", forbidden_tool)
    configuration = ProviderConfiguration(provider_id="mock")
    monkeypatch.setattr(runner, "provider_profile", lambda: {
        "id": "mock", "default_model": "siyi-mock-v1", "credential_policy": "forbidden",
        "effective_capabilities": {"local": True, "window_status": "declared"},
    })
    monkeypatch.setattr(runner, "provider_ready", lambda _api_key: True)
    monkeypatch.setattr("app.providers.model_routing.load_provider_configuration", lambda: configuration)
    monkeypatch.setattr("app.cognition.semantic_planner.load_provider_configuration", lambda: configuration)
    admission = RequestBudget(
        estimated_input_tokens=100, context_window_tokens=65_536, reserved_output_tokens=8192,
        provider_overhead_tokens=100, safety_margin_tokens=100, effective_input_budget=57_144,
        compaction_threshold_tokens=40_000, should_compact=False, exceeds_context_window=False,
    )
    monkeypatch.setattr("app.cognition.semantic_planner.request_budget", lambda *args, **kwargs: admission)
    monkeypatch.setattr(runner, "request_budget", lambda *args, **kwargs: admission)
    init_db()
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("synthetic ready planner", str(tmp_path), "ask", stamp, stamp),
        )
        conversation_id = int(cursor.lastrowid)
    return conversation_id, uuid.uuid4().hex


def _limits():
    return replace(
        runner.TaskLimits.current(), task_timeout_seconds=0.2,
        max_consecutive_failures=3, max_agent_rounds=2, max_repair_attempts=0,
    )


def _request(identity: tuple[int, str], **kwargs) -> ChatRequest:
    conversation_id, task_id = identity
    return ChatRequest(
        conversation_id=conversation_id, task_id=task_id, content="请回复你好", **kwargs,
    )


def _stored(task_id: str) -> tuple[dict, list[dict]]:
    with connect() as db:
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())
        checkpoints = [dict(row) for row in db.execute(
            "SELECT phase,reason,state FROM task_checkpoints WHERE task_id=? ORDER BY sequence", (task_id,),
        )]
    return task, checkpoints


def test_ready_planner_timeout_falls_back_with_checkpoint_before_executor(ready_planner):
    phases: list[str] = []
    cancelled: list[str] = []

    async def complete(messages, api_key=None, **kwargs):
        phase = kwargs["phase"]
        phases.append(phase)
        if phase == "planning":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(phase)
        with connect() as db:
            assert db.execute(
                "SELECT 1 FROM task_checkpoints WHERE task_id=? AND reason='planner_timeout'",
                (ready_planner[1],),
            ).fetchone() is not None
        return {"content": "你好。", "_metrics": {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}}

    async def scenario():
        return await asyncio.wait_for(runner.run_chat(
            _request(ready_planner), completion_fn=complete, limits=_limits(),
        ), timeout=4)

    result = asyncio.run(scenario())
    task_id = ready_planner[1]
    task, checkpoints = _stored(task_id)
    assert result["task_status"] == "completed"
    assert phases == ["planning", "analysis"]
    assert cancelled == ["planning"]
    assert task["model_calls"] == 2
    timeout_checkpoint = next(item for item in checkpoints if item["reason"] == "planner_timeout")
    timeout_state = json.loads(timeout_checkpoint["state"])
    assert timeout_state["consecutive_segment_timeouts"] == 1
    assert any(item.get("type") == "planner_fallback" and item.get("reason") == "planner_timeout"
               for item in timeout_state["known_errors"])
    with connect() as db:
        plan = json.loads(db.execute("SELECT plan FROM task_plans WHERE task_id=?", (task_id,)).fetchone()[0])
    assert plan["planner_source"] == "deterministic_fallback"
    assert plan["privacy_scope"] == "workspace"


@pytest.mark.parametrize("failure_limit", [1, 3])
def test_planner_and_executor_timeouts_share_failure_limit_and_drain_waits(ready_planner, failure_limit):
    phases: list[str] = []
    cancelled: list[str] = []
    active: set[int] = set()

    async def complete(messages, api_key=None, **kwargs):
        phase = kwargs["phase"]
        phases.append(phase)
        number = len(phases)
        active.add(number)
        try:
            await asyncio.Event().wait()
        finally:
            active.remove(number)
            cancelled.append(phase)

    async def scenario():
        result = await asyncio.wait_for(runner.run_chat(
            _request(ready_planner), completion_fn=complete,
            limits=replace(_limits(), max_consecutive_failures=failure_limit),
        ), timeout=4)
        assert active == set()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
        return result

    result = asyncio.run(scenario())
    task, checkpoints = _stored(ready_planner[1])
    assert result["task_status"] == "partially_completed"
    assert task["current_step"] == "repeated_model_timeout"
    assert "超时" in task["termination_reason"]
    assert task["resumable"] == 0
    assert phases == ["planning", *(["analysis"] * (failure_limit - 1))]
    assert cancelled == phases
    assert task["model_calls"] == failure_limit
    assert any(item["reason"] == "planner_timeout" for item in checkpoints)
    assert checkpoints[-1]["reason"] == "repeated_model_timeout"
    assert json.loads(checkpoints[-1]["state"])["consecutive_segment_timeouts"] == failure_limit


def test_user_cancel_during_planner_does_not_fall_back_or_call_executor(ready_planner):
    phases: list[str] = []

    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def complete(messages, api_key=None, **kwargs):
            phases.append(kwargs["phase"])
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        pending = asyncio.create_task(runner.run_chat(
            _request(ready_planner), completion_fn=complete,
            limits=replace(_limits(), task_timeout_seconds=5),
        ))
        await asyncio.wait_for(started.wait(), timeout=2)
        cancellation = runner.cancel_task(ready_planner[1])
        result = await asyncio.wait_for(pending, timeout=2)
        assert cancellation["interrupted"] is True
        assert cancelled.is_set()
        return result

    result = asyncio.run(scenario())
    task, checkpoints = _stored(ready_planner[1])
    assert result["task_status"] == "cancelled"
    assert phases == ["planning"]
    assert task["resumable"] == 0
    assert not any(item["reason"] in {"planner_timeout", "model_timeout"} for item in checkpoints)


def test_tighter_task_deadline_during_planner_stops_without_fallback(ready_planner):
    phases: list[str] = []
    cancelled: list[str] = []

    async def complete(messages, api_key=None, **kwargs):
        phases.append(kwargs["phase"])
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(kwargs["phase"])

    async def scenario():
        return await asyncio.wait_for(runner.run_chat(
            # This branch was already GREEN at 150 ms. Allow slow CI setup
            # ample room to enter planning before the tighter absolute limit.
            _request(ready_planner, task_deadline_at=datetime.now(timezone.utc) + timedelta(seconds=1)),
            completion_fn=complete, limits=replace(_limits(), task_timeout_seconds=5),
        ), timeout=4)

    result = asyncio.run(scenario())
    task, checkpoints = _stored(ready_planner[1])
    assert result["task_status"] == "timed_out"
    assert task["current_step"] == "task_deadline"
    assert task["resumable"] == 0
    assert phases == cancelled == ["planning"]
    assert not any(item["reason"] in {"planner_timeout", "model_timeout"} for item in checkpoints)


@pytest.mark.parametrize("error_type", sorted(COST_ERRORS))
def test_cost_error_after_planner_timeout_fallback_stops_without_retry(ready_planner, error_type):
    phases: list[str] = []

    async def complete(messages, api_key=None, **kwargs):
        phase = kwargs["phase"]
        phases.append(phase)
        if phase == "planning":
            await asyncio.Event().wait()
        raise ProviderError("synthetic cost guard; no request sent", error_type)

    async def scenario():
        return await asyncio.wait_for(runner.run_chat(
            _request(ready_planner), completion_fn=complete, limits=_limits(),
        ), timeout=4)

    result = asyncio.run(scenario())
    task, checkpoints = _stored(ready_planner[1])
    expected = TaskStatus.PARTIALLY_COMPLETED if error_type == "cost_budget_limit" else TaskStatus.WAITING_PROVIDER
    assert result["task_status"] == expected.value
    assert task["current_step"] == error_type
    assert phases == ["planning", "analysis"]
    assert task["tool_calls"] == 0
    assert task["files_modified"] == 0
    assert checkpoints[-1]["reason"] == "provider_interrupted"


def test_uncertain_planner_reservation_blocks_fallback_before_second_admission(ready_planner):
    """Exercise the real reservation/fuse guard, not a fabricated ProviderError."""
    price = {
        "version": 1, "provider": "synthetic-offline", "endpoint_hash": "a" * 16,
        "model": "synthetic", "rates": {"input": 1, "output": 2},
    }
    task_id = ready_planner[1]
    phases: list[str] = []
    admitted_calls: list[str] = []
    blocked_codes: list[str] = []
    original_reservations: list[dict] = []

    async def complete(messages, api_key=None, **kwargs):
        phase = kwargs["phase"]
        phases.append(phase)
        try:
            reservation = reserve(
                task_id=task_id, price=price, input_tokens=100, output_tokens=512,
            )
        except CostBudgetBlocked as exc:
            blocked_codes.append(exc.code)
            # The real Provider performs this same error translation before
            # opening its HTTP client; the fixture additionally forbids HTTP.
            raise ProviderError(str(exc), exc.code) from exc
        assert reservation is not None
        admitted_calls.append(phase)
        assert phase == "planning", "the fallback must not admit a second call"
        with connect() as db:
            snapshot = json.loads(db.execute(
                "SELECT price_snapshot_json FROM agent_tasks WHERE id=?", (task_id,),
            ).fetchone()[0])
        original_reservations.append(dict(snapshot[LEDGER_KEY][reservation.id]))
        try:
            await asyncio.Event().wait()
        finally:
            # A sent wait whose final accounting cannot settle must preserve
            # its full reservation. No known-cost receipt is manufactured.
            mark_uncertain(reservation)

    async def scenario():
        return await asyncio.wait_for(runner.run_chat(
            _request(ready_planner, cost_budget_limit=0.01),
            completion_fn=complete, limits=_limits(),
        ), timeout=4)

    result = asyncio.run(scenario())
    task, checkpoints = _stored(task_id)
    assert result["task_status"] == "waiting_provider"
    assert task["current_step"] == "cost_usage_unknown"
    assert task["cost_budget_limit"] == 0.01
    assert phases == ["planning", "analysis"]
    assert admitted_calls == ["planning"]
    assert blocked_codes == ["cost_usage_unknown"]
    assert task["tool_calls"] == task["files_modified"] == 0
    assert any(item["reason"] == "planner_timeout" for item in checkpoints)
    assert checkpoints[-1]["reason"] == "provider_interrupted"
    reservations = json.loads(task["price_snapshot_json"])[LEDGER_KEY]
    assert len(reservations) == 1
    retained = next(iter(reservations.values()))
    assert retained == {**original_reservations[0], "state": "uncertain"}
    assert Decimal(retained["upper_bound_usd"]) == Decimal("0.00112400")
    public_cost = task_cost_fields(task)
    assert public_cost["cost_status"] == "unknown"
    assert public_cost["estimated_cost_usd"] is None
    assert public_cost["pending_cost_requests"] == 1
    with connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_runs WHERE task_id=?", (task_id,)).fetchone()[0] == 0
