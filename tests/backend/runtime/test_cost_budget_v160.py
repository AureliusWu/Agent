"""Real SQLite leases/reservations with fully mocked, unpaid transport."""
import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest

from app.config import settings
from app.database import connect, init_db, now_iso, record_model_run, rows
from app.providers.costs import cost_summary, task_cost_fields, usage_snapshot
from app.providers.provider import ProviderError, completion
from app.runtime.cost_budget import CostBudgetBlocked, reserve, release_unsent, check_result
from app.runtime.task_leases import (acquire_task_lease, bind_task_lease, release_task_lease, reset_task_lease)

PRICE = {"version": 1, "provider": "api.deepseek.com", "endpoint_hash": "a" * 16,
         "model": "synthetic", "rates": {"input": 1, "output": 2}}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "costs.db")
    monkeypatch.setattr(settings, "model_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(settings, "model_pricing_json", '{"synthetic":{"input":1,"output":2}}')
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "false")
    init_db()


def task(budget=0.1):
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute("INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                   (conversation_id, "新对话", "", "ask", stamp, stamp))
        db.execute("INSERT INTO agent_tasks(id,conversation_id,status,prompt,cost_budget_limit,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (task_id, conversation_id, "running", "synthetic", budget, stamp, stamp))
    return task_id, conversation_id


@pytest.fixture
def leased(isolated):
    task_id, conversation_id = task()
    lease = acquire_task_lease(task_id, ttl_seconds=60)
    token = bind_task_lease(lease)
    try:
        yield task_id, conversation_id, lease
    finally:
        reset_task_lease(token)
        release_task_lease(lease)


def persist(task_id, conversation_id, usage, reservation=None, price=PRICE, success=True):
    snapshot = usage_snapshot(price, usage, successful=success)
    record_model_run(conversation_id=conversation_id, task_id=task_id, provider=price["provider"], model=price["model"],
                     started_at=now_iso(), duration_ms=1, usage=usage, success=success, error_type=None, retry_count=0,
                     estimated_cost_usd=float(snapshot["known_cost_usd_decimal"]), price_snapshot=snapshot, cost_reservation=reservation)


def test_usd_reserve_requires_active_root_lease_and_cannot_detach(leased):
    task_id, _, lease = leased
    for different in (None, "other"):
        with pytest.raises(CostBudgetBlocked, match="绑定") as exc:
            reserve(task_id=different, price=PRICE, input_tokens=10, output_tokens=10)
        assert exc.value.code == "cost_lease_required"
    token = bind_task_lease(None)
    try:
        with pytest.raises(CostBudgetBlocked) as exc:
            reserve(task_id=task_id, price=PRICE, input_tokens=10, output_tokens=10)
        assert exc.value.code == "cost_lease_required"
    finally:
        reset_task_lease(token)


def test_reservation_and_model_row_settle_atomically(leased):
    task_id, conversation_id, _ = leased
    reservation = reserve(task_id=task_id, price=PRICE, input_tokens=10, output_tokens=10)
    assert task_cost_fields(rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0])["estimated_cost_usd"] is None
    persist(task_id, conversation_id, {"prompt_tokens": 10, "completion_tokens": 5}, reservation)
    stored = rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))[0]
    assert stored["total_tokens"] == 15
    assert cost_summary([stored])["estimated_cost_usd"] == 0.00002
    assert task_cost_fields(rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0])["cost_status"] == "known"


def test_atomic_parallel_reservations_share_parent_budget(leased):
    task_id, _, lease = leased
    with connect() as db:
        db.execute("UPDATE agent_tasks SET cost_budget_limit=0.00002 WHERE id=?", (task_id,))

    def attempt(_):
        token = bind_task_lease(lease)
        try:
            return reserve(task_id=task_id, price=PRICE, input_tokens=0, output_tokens=10)
        except CostBudgetBlocked as exc:
            return exc.code
        finally:
            reset_task_lease(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(value == "cost_budget_limit" for value in results) == 1
    accepted = next(value for value in results if not isinstance(value, str))
    release_unsent(accepted)


def test_old_unknown_usage_cannot_become_free_after_configuring_rates(leased):
    task_id, conversation_id, _ = leased
    persist(task_id, conversation_id, {}, price={**PRICE, "rates": None})
    with pytest.raises(CostBudgetBlocked) as exc:
        reserve(task_id=task_id, price=PRICE, input_tokens=1, output_tokens=1)
    assert exc.value.code == "cost_usage_unknown"


def test_crashed_generation_pending_is_not_released_on_resume(leased):
    task_id, _, lease = leased
    reserve(task_id=task_id, price=PRICE, input_tokens=1, output_tokens=1)
    release_task_lease(lease)
    replacement = acquire_task_lease(task_id, ttl_seconds=60)
    token = bind_task_lease(replacement)
    try:
        with pytest.raises(CostBudgetBlocked) as exc:
            reserve(task_id=task_id, price=PRICE, input_tokens=1, output_tokens=1)
        assert exc.value.code == "cost_usage_unknown"
    finally:
        reset_task_lease(token)
        release_task_lease(replacement)


def test_settlement_lost_lease_rolls_back_model_insert_and_retains_reservation(leased):
    task_id, conversation_id, lease = leased
    reservation = reserve(task_id=task_id, price=PRICE, input_tokens=1, output_tokens=1)
    release_task_lease(lease)
    with pytest.raises(Exception):
        persist(task_id, conversation_id, {"prompt_tokens": 1, "completion_tokens": 1}, reservation)
    assert not rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))
    assert task_cost_fields(rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0])["pending_cost_requests"] == 1


def test_check_result_fences_other_unknown_call(leased):
    task_id, conversation_id, _ = leased
    persist(task_id, conversation_id, {})
    with pytest.raises(CostBudgetBlocked) as exc:
        check_result(task_id, {"cost_status": "known"})
    assert exc.value.code == "cost_usage_unknown"


def test_legacy_missing_task_snapshot_column_fails_closed_without_schema_change(leased):
    task_id, _, _ = leased
    with connect() as db:
        db.execute("ALTER TABLE agent_tasks DROP COLUMN price_snapshot_json")
        db.execute("DELETE FROM schema_migrations WHERE version=46")
    init_db()
    with pytest.raises(CostBudgetBlocked) as exc:
        reserve(task_id=task_id, price=PRICE, input_tokens=1, output_tokens=1)
    assert exc.value.code == "cost_usage_unknown"
    with connect() as db:
        assert "price_snapshot_json" not in {row[1] for row in db.execute("PRAGMA table_info(agent_tasks)")}
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 46


@pytest.fixture
def transport(monkeypatch):
    calls = []

    async def validate(*args, **kwargs):
        return None

    async def response(client, method, url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "synthetic"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    monkeypatch.setattr("app.providers.provider.validate_outbound_url", validate)
    monkeypatch.setattr("app.providers.provider.guarded_request", response)
    return calls


def test_pricing_unknown_preflight_never_sends_request(leased, transport, monkeypatch):
    task_id, conversation_id, _ = leased
    monkeypatch.setattr(settings, "model_pricing_json", "{}")
    with pytest.raises(ProviderError) as exc:
        asyncio.run(completion([{"role": "user", "content": "synthetic"}], "synthetic-key", model="synthetic", max_tokens=16,
                               task_id=task_id, conversation_id=conversation_id))
    assert exc.value.error_type == "cost_pricing_unknown"
    assert transport == []


def test_successful_priced_transport_returns_known_and_freezes_snapshot(leased, transport):
    task_id, conversation_id, _ = leased
    message = asyncio.run(completion([{"role": "user", "content": "synthetic"}], "synthetic-key", model="synthetic", max_tokens=16,
                                     task_id=task_id, conversation_id=conversation_id))
    assert message["_metrics"]["cost_status"] == "known"
    assert message["_metrics"]["estimated_cost_usd"] == 0.000014
    assert len(transport) == 1
    assert task_cost_fields(rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0])["cost_status"] == "known"


def test_missing_usage_response_stops_before_returning_tools_and_records_once(leased, transport, monkeypatch):
    task_id, conversation_id, _ = leased

    async def missing(*args, **kwargs):
        transport.append(kwargs["json"])
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "x", "type": "function",
          "function": {"name": "write_file", "arguments": "{}"}}]}}]})

    monkeypatch.setattr("app.providers.provider.guarded_request", missing)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(completion([{"role": "user", "content": "synthetic"}], "synthetic-key", model="synthetic", max_tokens=16,
                               task_id=task_id, conversation_id=conversation_id))
    assert exc.value.error_type == "cost_usage_unknown"
    stored = rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))
    assert len(stored) == 1
    assert cost_summary(stored)["estimated_cost_usd"] is None
    assert task_cost_fields(rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0])["pending_cost_requests"] == 0


def test_dollar_bounded_transport_does_not_retry_http_failure(leased, transport, monkeypatch):
    task_id, conversation_id, _ = leased

    async def failed(*args, **kwargs):
        transport.append(kwargs["json"])
        return httpx.Response(500)

    monkeypatch.setattr("app.providers.provider.guarded_request", failed)
    with pytest.raises(ProviderError):
        asyncio.run(completion([{"role": "user", "content": "synthetic"}], "synthetic-key", model="synthetic", max_tokens=16,
                               task_id=task_id, conversation_id=conversation_id, max_retries=5))
    assert len(transport) == 1
    assert cost_summary(rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,)))["cost_status"] == "unknown"


def test_multimodal_input_has_no_unproven_text_cost_upper_bound(leased, transport):
    task_id, conversation_id, _ = leased
    with pytest.raises(ProviderError) as exc:
        asyncio.run(completion([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/image"}}]}],
                               "synthetic-key", model="synthetic", max_tokens=16, task_id=task_id, conversation_id=conversation_id))
    assert exc.value.error_type == "cost_input_unknown"
    assert transport == []


def test_cost_persistence_failure_retains_uncertain_pending_and_propagates_guard(leased, transport, monkeypatch):
    task_id, conversation_id, _ = leased

    def broken(**kwargs):
        raise RuntimeError("synthetic storage unavailable")

    monkeypatch.setattr("app.providers.provider.record_model_run", broken)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(completion([{"role": "user", "content": "synthetic"}], "synthetic-key", model="synthetic", max_tokens=16,
                               task_id=task_id, conversation_id=conversation_id))
    assert exc.value.error_type == "cost_usage_unknown"
    assert len(transport) == 1
    with pytest.raises(CostBudgetBlocked) as blocked:
        reserve(task_id=task_id, price=PRICE, input_tokens=1, output_tokens=1)
    assert blocked.value.code == "cost_usage_unknown"
    assert task_cost_fields(rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0])["pending_cost_requests"] == 1


def test_final_payload_estimate_not_caller_low_estimate_controls_preflight(leased, transport):
    task_id, conversation_id, _ = leased
    with connect() as db:
        db.execute("UPDATE agent_tasks SET cost_budget_limit=0.000001 WHERE id=?", (task_id,))
    with pytest.raises(ProviderError) as exc:
        asyncio.run(completion([{"role": "user", "content": "中文" * 1000}], "synthetic-key", model="synthetic", max_tokens=16,
                               estimated_input_tokens=0, task_id=task_id, conversation_id=conversation_id))
    assert exc.value.error_type == "cost_budget_limit"
    assert transport == []


def test_known_actual_cost_plus_pending_reservation_cannot_exceed_budget(leased):
    task_id, conversation_id, _ = leased
    with connect() as db:
        db.execute("UPDATE agent_tasks SET cost_budget_limit=0.00004 WHERE id=?", (task_id,))
    reserve(task_id=task_id, price=PRICE, input_tokens=0, output_tokens=10)
    persist(task_id, conversation_id, {"prompt_tokens": 30, "completion_tokens": 0})
    with pytest.raises(CostBudgetBlocked) as exc:
        check_result(task_id, {"cost_status": "known"})
    assert exc.value.code == "cost_budget_limit"


def test_planner_does_not_swallow_cost_guard(leased):
    from app.cognition.semantic_planner import build_semantic_task_plan, PlannerContext
    task_id, conversation_id, _ = leased

    async def blocked(*args, **kwargs):
        raise ProviderError("synthetic cost stop", "cost_pricing_unknown")

    with pytest.raises(ProviderError):
        asyncio.run(build_semantic_task_plan(task_id, "分析文件", ["read_file"], complete=blocked, api_key=None,
                                            context=PlannerContext(), conversation_id=conversation_id))


def test_parallel_child_cost_guard_cancels_and_drains_siblings(leased, tmp_path):
    from app.runtime.multi_agent import ensure_root_agent, run_orchestration_prelude, READ_ONLY_CHILD_TOOLS, task_agent_trace
    from app.cognition.planning import build_task_plan
    task_id, conversation_id, _ = leased
    ensure_root_agent(task_id, "parallel_explorers", objective="synthetic", token_budget=1000,
                      tool_allowlist=READ_ONLY_CHILD_TOOLS, file_scope=("**",), timeout_seconds=30)
    started = asyncio.Event()
    cancelled = []
    calls = 0

    async def completion_fn(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await started.wait()
            raise ProviderError("synthetic cost stop", "cost_budget_limit")
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    with pytest.raises(ProviderError):
        asyncio.run(run_orchestration_prelude(task_id=task_id, mode="parallel_explorers", agent_count=2, prompt="只读分析工作区",
                     plan=build_task_plan(task_id, "只读分析工作区", READ_ONLY_CHILD_TOOLS), conversation_id=conversation_id,
                     workspace=str(tmp_path), api_key=None, completion_fn=completion_fn))
    assert cancelled == [True]
    assert all(agent["status"] != "running" for agent in task_agent_trace(task_id)["agents"] if agent["depth"] > 0)


def test_positive_budget_title_uses_local_fallback_even_in_recovery(leased):
    from app.artifacts.title_jobs import ensure_title_job, run_title_job, schedule_title_generation
    task_id, conversation_id, _ = leased
    with connect() as db:
        db.execute("INSERT INTO messages(conversation_id,task_id,role,content,created_at) VALUES(?,?,?,?,?)",
                   (conversation_id, task_id, "assistant", "synthetic", now_iso()))
    job = ensure_title_job(conversation_id, "修复文件内容", "完成")

    async def must_not_call(*args, **kwargs):
        pytest.fail("Dollar-bounded title must not call a paid model")

    result = asyncio.run(run_title_job(job["id"], completion_fn=must_not_call))
    assert result["title_source"] == "fallback"
    assert result["attempts"] == 0
    assert schedule_title_generation(conversation_id, "修复文件内容", "完成") is None


def test_cancelled_settlement_db_failure_blocks_cloned_same_generation_and_preserves_cost_cause(leased, transport, monkeypatch):
    from dataclasses import replace
    from app.runtime import cost_budget
    task_id, conversation_id, lease = leased
    real_connect = cost_budget.connect
    database_down = False

    def failing_record(**kwargs):
        raise OSError("synthetic cost database not writable")

    def cost_connect():
        if database_down:
            raise OSError("synthetic cost database not writable")
        return real_connect()

    async def cancelled_request(*args, **kwargs):
        nonlocal database_down
        transport.append(kwargs["json"])
        database_down = True
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.providers.provider.guarded_request", cancelled_request)
    monkeypatch.setattr("app.providers.provider.record_model_run", failing_record)
    monkeypatch.setattr(cost_budget, "connect", cost_connect)
    with pytest.raises(ProviderError) as error:
        asyncio.run(completion([{"role": "user", "content": "synthetic"}], "synthetic-key", model="synthetic", max_tokens=16,
                               task_id=task_id, conversation_id=conversation_id))
    assert error.value.error_type == "cost_usage_unknown"
    assert isinstance(error.value.__cause__, OSError)
    assert isinstance(error.value.__cause__.__context__, asyncio.CancelledError)
    database_down = False
    pending = json.loads(rows("SELECT price_snapshot_json FROM agent_tasks WHERE id=?", (task_id,))[0]["price_snapshot_json"])
    assert "uncertain" not in str(pending)  # Durable marking also failed.
    cloned = replace(lease, expires_at=lease.expires_at + 60)
    token = bind_task_lease(cloned)
    try:
        with pytest.raises(CostBudgetBlocked) as stopped:
            reserve(task_id=task_id, price=PRICE, input_tokens=1, output_tokens=1)
        assert stopped.value.code == "cost_usage_unknown"
        with pytest.raises(CostBudgetBlocked) as stopped_result:
            check_result(task_id, {"cost_status": "known"})
        assert stopped_result.value.code == "cost_usage_unknown"
    finally:
        reset_task_lease(token)
    assert len(transport) == 1
    assert not rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))


def test_child_cost_fatal_not_masked_by_terminal_or_performance_write_failures(leased, tmp_path, monkeypatch):
    from app.runtime import multi_agent
    from app.cognition.planning import build_task_plan
    task_id, conversation_id, _ = leased
    multi_agent.ensure_root_agent(task_id, "parallel_explorers", objective="synthetic", token_budget=1000,
        tool_allowlist=multi_agent.READ_ONLY_CHILD_TOOLS, file_scope=("**",), timeout_seconds=30)
    original = ProviderError("synthetic unconfirmed cancelled charge", "cost_usage_unknown")
    calls = 0
    sibling_cancelled = []
    started = asyncio.Event()

    async def completion_fn(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await started.wait()
            raise original
        started.set()
        try:
            await asyncio.sleep(0.1)
            return {"content": "must not continue after sibling fatal"}
        except asyncio.CancelledError:
            sibling_cancelled.append(True)
            raise

    def failed_log(*args, **kwargs):
        raise OSError("synthetic metadata database not writable")

    monkeypatch.setattr(multi_agent, "_finish_child", failed_log)
    monkeypatch.setattr(multi_agent, "record_performance_trace", failed_log)
    with pytest.raises(ProviderError) as error:
        asyncio.run(multi_agent.run_orchestration_prelude(task_id=task_id, mode="parallel_explorers", agent_count=2,
            prompt="只读分析工作区", plan=build_task_plan(task_id, "只读分析工作区", multi_agent.READ_ONLY_CHILD_TOOLS),
            conversation_id=conversation_id, workspace=str(tmp_path), api_key=None, completion_fn=completion_fn))
    assert error.value is original
    assert sibling_cancelled == [True]


def test_shared_fuse_is_bound_to_database_task_and_generation(leased, monkeypatch, tmp_path):
    from dataclasses import replace
    from app.runtime import cost_budget
    task_id, _, lease = leased
    monkeypatch.setattr(cost_budget, "_uncertain_leases", set())
    monkeypatch.setattr(cost_budget, "_uncertain_capacity_exhausted", False)
    cost_budget._trip_lease(task_id, lease.generation)
    with pytest.raises(CostBudgetBlocked):
        cost_budget._require_unfused_lease(replace(lease, expires_at=lease.expires_at + 60))
    cost_budget._require_unfused_lease(replace(lease, generation=lease.generation + 1))
    cost_budget._require_unfused_lease(replace(lease, task_id="another-task"))
    with monkeypatch.context() as different_database:
        different_database.setattr(settings, "database_path", tmp_path / "another-database.db")
        cost_budget._require_unfused_lease(lease)


def test_shared_fuse_capacity_is_bounded_and_only_saturation_blocks_positive_usd(leased, monkeypatch):
    from app.runtime import cost_budget
    task_id, _, lease = leased
    monkeypatch.setattr(cost_budget, "MAX_UNCERTAIN_LEASES", 1)
    monkeypatch.setattr(cost_budget, "_uncertain_leases", set())
    monkeypatch.setattr(cost_budget, "_uncertain_capacity_exhausted", False)
    cost_budget._trip_lease("first-failed-task", 1)
    cost_budget._trip_lease("second-failed-task", 1)
    assert len(cost_budget._uncertain_leases) == 1
    assert cost_budget._uncertain_capacity_exhausted is True
    with pytest.raises(CostBudgetBlocked) as capped:
        reserve(task_id=task_id, price=PRICE, input_tokens=1, output_tokens=1)
    assert capped.value.code == "cost_usage_unknown"
    uncapped_task, _ = task(budget=None)
    token = bind_task_lease(None)
    try:
        assert reserve(task_id=uncapped_task, price=PRICE, input_tokens=1, output_tokens=1) is None
    finally:
        reset_task_lease(token)
