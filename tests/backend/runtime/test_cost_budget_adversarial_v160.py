"""Independent cost-boundary counterexamples; synthetic SQLite and offline transport.

These exercise the production Provider and reservation guards without a real
credential, socket, model, workspace operation, or paid request.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.database import connect, init_db, now_iso, rows
from app.providers.costs import LEDGER_KEY, pricing_snapshot, public_task
from app.providers.provider import ProviderError, completion
from app.runtime.cost_budget import CostBudgetBlocked, reserve
from app.runtime.task_leases import (
    TaskLeaseConflict,
    acquire_task_lease,
    bind_task_lease,
    release_task_lease,
    reset_task_lease,
)


@pytest.fixture
def isolated_cost_parent(tmp_path: Path, monkeypatch) -> tuple[int, str]:
    monkeypatch.setattr(settings, "database_path", tmp_path / "cost-adversarial.db")
    monkeypatch.setattr(settings, "model_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(settings, "model_pricing_json", '{"adversarial-model":{"input":1,"output":1}}')
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "mock")
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "false")
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))
    init_db()
    stamp = now_iso()
    task_id = uuid.uuid4().hex
    with connect() as db:
        conversation = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("synthetic cost boundary", str(tmp_path), "ask", stamp, stamp),
        )
        conversation_id = int(conversation.lastrowid)
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,cost_budget_limit,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "synthetic public input", 0.01, stamp, stamp),
        )
    return conversation_id, task_id


class _OfflineClient:
    def __init__(self, **kwargs: Any) -> None:
        assert kwargs["follow_redirects"] is False

    async def __aenter__(self) -> _OfflineClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _OfflineResponse:
    status_code = 200

    def __init__(self, *, include_usage: bool = True) -> None:
        self.include_usage = include_usage

    def json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": "public result"}}]}
        if self.include_usage:
            body["usage"] = {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}
        return body


def _offline(monkeypatch, request) -> None:
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", _OfflineClient)
    monkeypatch.setattr("app.providers.provider.guarded_request", request)


async def _call(conversation_id: int, task_id: str | None, text: str = "public") -> dict[str, Any]:
    return await completion(
        [{"role": "user", "content": text}],
        tools=[],
        base_url="https://api.deepseek.com",
        model="adversarial-model",
        max_tokens=16,
        max_retries=5,
        conversation_id=conversation_id,
        task_id=task_id,
        credential_policy="forbidden",
    )


@pytest.mark.parametrize("detached_id", [None, "not-a-task", "unbounded-other-task"])
def test_inherited_bounded_parent_cannot_detach_task_identity(
    isolated_cost_parent, monkeypatch, detached_id: str | None,
) -> None:
    conversation_id, task_id = isolated_cost_parent
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("unbounded-other-task", conversation_id, "running", "another synthetic task", stamp, stamp),
        )
    requests = 0

    async def request(*args, **kwargs):
        nonlocal requests
        requests += 1
        pytest.fail("detached bounded task reached the offline HTTP boundary")

    _offline(monkeypatch, request)

    async def scenario() -> None:
        lease = acquire_task_lease(task_id)
        token = bind_task_lease(lease)
        try:
            # Child tasks inherit ContextVar state; omission or another ID must
            # not turn a bounded parent into an unbounded model call.
            child = asyncio.create_task(_call(conversation_id, detached_id))
            with pytest.raises(ProviderError) as error:
                await child
            assert error.value.error_type == "cost_lease_required"
        finally:
            release_task_lease(lease)
            reset_task_lease(token)

    asyncio.run(scenario())
    assert requests == 0
    assert rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,)) == []
    assert json.loads(rows("SELECT price_snapshot_json FROM agent_tasks WHERE id=?", (task_id,))[0]["price_snapshot_json"]) == {}


def test_lease_drift_after_send_retains_durable_reservation_and_blocks_resume(
    isolated_cost_parent, monkeypatch,
) -> None:
    conversation_id, task_id = isolated_cost_parent
    requests = 0
    owned_lease = None

    async def request(*args, **kwargs):
        nonlocal requests
        requests += 1
        assert "Authorization" not in kwargs.get("headers", {})
        # A real request may already be billable when the owner is lost.
        assert owned_lease is not None
        assert release_task_lease(owned_lease)
        return _OfflineResponse()

    _offline(monkeypatch, request)

    async def scenario() -> None:
        nonlocal owned_lease
        owned_lease = acquire_task_lease(task_id)
        token = bind_task_lease(owned_lease)
        try:
            with pytest.raises(ProviderError) as drift_error:
                await _call(conversation_id, task_id)
            assert drift_error.value.error_type == "cost_usage_unknown"
            assert isinstance(drift_error.value.__cause__, TaskLeaseConflict)
        finally:
            reset_task_lease(token)
        persisted = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0]
        pending = json.loads(persisted["price_snapshot_json"])[LEDGER_KEY]
        assert len(pending) == 1
        assert next(iter(pending.values()))["generation"] == owned_lease.generation
        assert public_task(persisted)["estimated_cost_usd"] is None
        fresh = acquire_task_lease(task_id)
        fresh_token = bind_task_lease(fresh)
        try:
            assert fresh.generation > owned_lease.generation
            with pytest.raises(ProviderError) as error:
                await _call(conversation_id, task_id)
            assert error.value.error_type == "cost_usage_unknown"
        finally:
            release_task_lease(fresh)
            reset_task_lease(fresh_token)

    asyncio.run(scenario())
    assert requests == 1
    assert rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,)) == []


def test_cancelled_sent_request_stays_unknown_and_cannot_restart_as_zero(
    isolated_cost_parent, monkeypatch,
) -> None:
    conversation_id, task_id = isolated_cost_parent
    requests = 0

    async def scenario() -> None:
        nonlocal requests
        sent = asyncio.Event()

        async def request(*args, **kwargs):
            nonlocal requests
            requests += 1
            sent.set()
            await asyncio.Event().wait()
            pytest.fail("cancelled offline request resumed")

        _offline(monkeypatch, request)
        lease = acquire_task_lease(task_id)
        token = bind_task_lease(lease)
        try:
            pending = asyncio.create_task(_call(conversation_id, task_id))
            await asyncio.wait_for(sent.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            runs = rows("SELECT * FROM model_runs WHERE task_id=?", (task_id,))
            assert len(runs) == 1
            assert runs[0]["error_type"] == "cancelled"
            assert json.loads(runs[0]["price_snapshot_json"])["cost_status"] == "unknown"
            assert public_task(rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0])["estimated_cost_usd"] is None
        finally:
            release_task_lease(lease)
            reset_task_lease(token)
        fresh = acquire_task_lease(task_id)
        fresh_token = bind_task_lease(fresh)
        try:
            with pytest.raises(ProviderError) as error:
                await _call(conversation_id, task_id)
            assert error.value.error_type == "cost_usage_unknown"
        finally:
            release_task_lease(fresh)
            reset_task_lease(fresh_token)

    asyncio.run(scenario())
    assert requests == 1


def test_interrupted_before_settlement_is_not_released_by_a_new_generation(
    isolated_cost_parent,
) -> None:
    _, task_id = isolated_cost_parent
    price = pricing_snapshot(provider="api.deepseek.com", base_url=settings.model_base_url, model="adversarial-model")
    old = acquire_task_lease(task_id)
    old_token = bind_task_lease(old)
    try:
        reservation = reserve(task_id=task_id, price=price, input_tokens=100, output_tokens=16)
        assert reservation is not None
    finally:
        # Deliberate local failure point: an owner ends without a usage receipt.
        # This is not a claim that a process-crash or real billing test ran.
        release_task_lease(old)
        reset_task_lease(old_token)
    fresh = acquire_task_lease(task_id)
    token = bind_task_lease(fresh)
    try:
        with pytest.raises(CostBudgetBlocked) as error:
            reserve(task_id=task_id, price=price, input_tokens=100, output_tokens=16)
        assert error.value.code == "cost_usage_unknown"
        pending = json.loads(rows("SELECT price_snapshot_json FROM agent_tasks WHERE id=?", (task_id,))[0]["price_snapshot_json"])[LEDGER_KEY]
        assert list(pending) == [reservation.id]
    finally:
        release_task_lease(fresh)
        reset_task_lease(token)


def test_concurrent_unknown_charge_stops_other_known_response_before_dispatch(
    isolated_cost_parent, monkeypatch,
) -> None:
    conversation_id, task_id = isolated_cost_parent
    requests = 0
    dispatched = []

    async def scenario() -> None:
        nonlocal requests
        both_sent = asyncio.Event()
        release_known = asyncio.Event()

        async def request(*args, **kwargs):
            nonlocal requests
            requests += 1
            assert "Authorization" not in kwargs.get("headers", {})
            if requests == 2:
                both_sent.set()
            if kwargs["json"]["messages"][0]["content"] == "unknown":
                await both_sent.wait()
                return _OfflineResponse(include_usage=False)
            await release_known.wait()
            return _OfflineResponse()

        async def consume(text: str) -> None:
            await _call(conversation_id, task_id, text)
            # A response cannot reach tool dispatch when another parallel call
            # made the same root's bill uncertain.
            dispatched.append(text)

        _offline(monkeypatch, request)
        lease = acquire_task_lease(task_id)
        token = bind_task_lease(lease)
        unknown = known = None
        try:
            unknown = asyncio.create_task(consume("unknown"))
            known = asyncio.create_task(consume("known"))
            await asyncio.wait_for(both_sent.wait(), 2)
            with pytest.raises(ProviderError) as unknown_error:
                await unknown
            assert unknown_error.value.error_type == "cost_usage_unknown"
            release_known.set()
            with pytest.raises(ProviderError) as known_error:
                await known
            assert known_error.value.error_type == "cost_usage_unknown"
        finally:
            for job in (unknown, known):
                if job is not None and not job.done():
                    job.cancel()
            await asyncio.gather(*(job for job in (unknown, known) if job is not None), return_exceptions=True)
            release_task_lease(lease)
            reset_task_lease(token)

    asyncio.run(scenario())
    assert requests == 2
    assert dispatched == []
    runs = rows("SELECT * FROM model_runs WHERE task_id=? ORDER BY id", (task_id,))
    assert len(runs) == 2
    assert [json.loads(run["price_snapshot_json"])["cost_status"] for run in runs] == ["unknown", "known"]
    assert public_task(rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0])["estimated_cost_usd"] is None
