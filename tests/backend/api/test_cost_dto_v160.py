import asyncio
import uuid

import pytest

from app.config import settings
from app.database import connect, init_db, now_iso, record_model_run
from app.providers.costs import usage_snapshot
from app.api.routes.system import usage_summary, recent_tasks
from app.api.routes.chat import task_runtime_status, recoverable_tasks
from app.runtime.task_runtime import task_snapshot, list_tasks


@pytest.fixture
def synthetic(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "dto.db")
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "false")
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute("INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                   (conversation_id, "synthetic", "", "ask", stamp, stamp))
        db.execute("INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                   (task_id, conversation_id, "waiting_provider", "synthetic", stamp, stamp))
    return task_id, conversation_id


def record(task_id, conversation_id, known):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    price = {"version": 1, "provider": "synthetic", "endpoint_hash": "a" * 16, "model": "synthetic",
             "rates": {"input": 1, "output": 2} if known else None}
    snapshot = usage_snapshot(price, usage)
    record_model_run(conversation_id=conversation_id, task_id=task_id, provider="synthetic", model="synthetic",
                     started_at=now_iso(), duration_ms=1, usage=usage, success=True, error_type=None, retry_count=0,
                     phase="analysis", price_snapshot=snapshot, estimated_cost_usd=float(snapshot["known_cost_usd_decimal"]))


def test_known_model_row_remains_known_in_all_task_dtos(synthetic):
    task_id, conversation_id = synthetic
    record(task_id, conversation_id, True)
    summaries = [task_snapshot(task_id), list_tasks(conversation_id)[0], recent_tasks()[0],
                 asyncio.run(recoverable_tasks(conversation_id))[0], asyncio.run(task_runtime_status(task_id))["limits"]]
    for summary in summaries:
        assert summary["cost_status"] == "known"
        assert summary["estimated_cost_usd"] == 0.00002
        assert summary["unknown_cost_requests"] == 0
        assert "price_snapshot_json" not in summary
    run = recent_tasks()[0]["model_runs"][0]
    assert run["estimated_cost_usd"] == 0.00002 and "price_snapshot_json" not in run


def test_aggregates_preserve_nullable_total_and_known_subtotal(synthetic):
    task_id, conversation_id = synthetic
    record(task_id, conversation_id, True)
    record(task_id, conversation_id, False)
    summary = usage_summary(1)
    costs = [summary["totals"], summary["models"][0], summary["daily"][0], recent_tasks()[0],
             recent_tasks()[0]["phase_costs"]["analysis"], asyncio.run(task_runtime_status(task_id))["limits"]]
    for cost in costs:
        assert cost["cost_status"] == "partial"
        assert cost["estimated_cost_usd"] is None
        assert cost["known_cost_usd"] == 0.00002
        assert cost["unknown_cost_requests"] == 1


def test_empty_history_is_zero_but_corrupt_pending_ledger_is_unknown(synthetic):
    task_id, _ = synthetic
    assert task_snapshot(task_id)["estimated_cost_usd"] == 0
    with connect() as db:
        db.execute("UPDATE agent_tasks SET price_snapshot_json='broken' WHERE id=?", (task_id,))
    assert task_snapshot(task_id)["estimated_cost_usd"] is None
    assert usage_summary()["totals"]["estimated_cost_usd"] is None


def test_historical_empty_price_snapshot_is_not_repriced_from_current_settings(synthetic, monkeypatch):
    task_id, conversation_id = synthetic
    record(task_id, conversation_id, True)
    with connect() as db:
        db.execute("UPDATE model_runs SET price_snapshot_json='{}',estimated_cost_usd=0 WHERE task_id=?", (task_id,))
    monkeypatch.setattr(settings, "model_pricing_json", '{"synthetic":{"input":1,"output":2}}')
    assert task_snapshot(task_id)["estimated_cost_usd"] is None
    assert usage_summary()["totals"]["cost_status"] == "unknown"
