"""Deterministic lease-drain cancellation checks; no model or external I/O."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import time
from unittest.mock import Mock
import uuid

import pytest

from app.config import settings
from app.database import connect, init_db, now_iso
from app.runtime import cancellation, runner
from app.runtime.task_leases import (
    acquire_task_lease,
    bind_task_lease,
    current_task_lease,
    release_task_lease,
    reset_task_lease,
)
from app.schemas import ChatRequest


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "cleanup.db")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "tavily_api_key", "")
    monkeypatch.setattr(settings, "brave_api_key", "")
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "absent-provider.json"))
    init_db()
    return tmp_path


def conversation(workspace: str = "") -> int:
    stamp = now_iso()
    with connect() as db:
        return int(db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("synthetic lease cleanup", workspace, "readonly", stamp, stamp),
        ).lastrowid)


def running_task() -> str:
    conversation_id = conversation()
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "synthetic cleanup", stamp, stamp),
        )
    return task_id


def lease_row(task_id: str) -> dict[str, object]:
    with connect() as db:
        return dict(db.execute(
            "SELECT owner_instance_id,token_hash,generation,status FROM task_leases WHERE task_id=?",
            (task_id,),
        ).fetchone())


def test_outer_cancel_during_heartbeat_drain_releases_exactly_once_and_propagates(isolated_database, monkeypatch):
    lease = acquire_task_lease(running_task())
    released = Mock(wraps=release_task_lease)
    monkeypatch.setattr(runner, "release_task_lease", released)

    async def exercise() -> None:
        drain_started = asyncio.Event()
        allow_drain = asyncio.Event()

        async def heartbeat() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                drain_started.set()
                await allow_drain.wait()

        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        finishing = asyncio.create_task(runner._finish_task_lease(lease, pulse, status="released"))
        try:
            await asyncio.wait_for(drain_started.wait(), timeout=1)
            finishing.cancel("synthetic caller cancellation")
            with pytest.raises(asyncio.CancelledError, match="synthetic caller cancellation"):
                await finishing
            assert pulse.done()
            released.assert_called_once_with(lease, status="released")
            assert lease_row(lease.task_id)["status"] == "released"
        finally:
            allow_drain.set()
            pulse.cancel()
            finishing.cancel()
            await asyncio.gather(pulse, finishing, return_exceptions=True)
            release_task_lease(lease)

    asyncio.run(exercise())


@pytest.mark.parametrize("mismatch", ["token", "generation", "replacement"])
def test_cleanup_native_fence_cannot_release_another_lease(isolated_database, mismatch):
    original = acquire_task_lease(running_task())
    current = original
    stale = original
    if mismatch == "token":
        stale = replace(original, token="synthetic-wrong-token")
    elif mismatch == "generation":
        stale = replace(original, generation=original.generation + 1)
    else:
        with connect() as db:
            db.execute("UPDATE task_leases SET expires_at=? WHERE task_id=?", (time.time() - 1, original.task_id))
        current = acquire_task_lease(original.task_id)
        assert current.generation == original.generation + 1
    before = lease_row(original.task_id)
    try:
        asyncio.run(runner._finish_task_lease(stale, None, status="released"))
        assert lease_row(original.task_id) == before
        assert before["status"] == "active"
    finally:
        release_task_lease(current)


@pytest.mark.parametrize("branch", ["unknown_context", "workspace_free", "workspace"])
@pytest.mark.parametrize("fault", ["cancel", "error", "reset_error"])
def test_real_run_chat_cleans_local_ownership_even_when_finish_raises(isolated_database, monkeypatch, branch, fault):
    root = isolated_database / "workspace"
    root.mkdir()
    conversation_id = conversation(str(root) if branch == "workspace" else "")
    task_id = uuid.uuid4().hex
    monkeypatch.setattr(runner, "provider_profile", lambda: {
        "id": "synthetic-mock",
        "effective_capabilities": {"local": branch == "unknown_context", "window_status": "unknown" if branch == "unknown_context" else "declared"},
    })
    model_calls = []
    boundaries = []
    bound_tokens = []
    leases = []
    real_bind = bind_task_lease
    released_token = Mock(wraps=cancellation.release_task_token)
    finalized_root = Mock(wraps=runner.finalize_root_agent)
    monkeypatch.setattr(runner, "release_task_token", released_token)
    monkeypatch.setattr(runner, "finalize_root_agent", finalized_root)

    def capture_bind(lease):
        token = real_bind(lease)
        bound_tokens.append(token)
        return token

    monkeypatch.setattr(runner, "bind_task_lease", capture_bind)

    async def forbidden_model(*args, **kwargs):
        model_calls.append(1)
        pytest.fail("cleanup probes must not invoke a model")

    async def workspace_free(*args, **kwargs):
        boundaries.append("workspace_free")
        return {"task_status": "waiting_provider"}

    async def workspace_boundary(*args, **kwargs):
        boundaries.append("workspace")
        raise RuntimeError("synthetic workspace boundary")

    monkeypatch.setattr(runner, "_run_workspace_free_conversation", workspace_free)
    monkeypatch.setattr(runner, "discover_mcp_tools", workspace_boundary)

    async def broken_finish(lease, heartbeat, *, status):
        leases.append(lease)
        assert current_task_lease(task_id) == lease
        assert runner._running_tasks.get(task_id) is asyncio.current_task()
        assert cancellation.task_token(task_id, create=False) is not None
        runner._lease_loss_requests[task_id] = "synthetic lease-loss marker"
        runner._shutdown_requests.add(task_id)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        if fault == "cancel":
            raise asyncio.CancelledError("synthetic finish cancellation")
        if fault == "error":
            raise RuntimeError("synthetic finish error")
        release_task_lease(lease, status=status)

    monkeypatch.setattr(runner, "_finish_task_lease", broken_finish)
    if fault == "reset_error":
        def broken_reset(token):
            raise RuntimeError("synthetic reset error")
        monkeypatch.setattr(runner, "reset_task_lease", broken_reset)

    async def exercise() -> None:
        expected = asyncio.CancelledError if fault == "cancel" else RuntimeError
        try:
            with pytest.raises(expected, match="synthetic (finish|reset)"):
                await runner.run_chat(
                    ChatRequest(conversation_id=conversation_id, task_id=task_id, content="synthetic cleanup request",
                                orchestration_mode="planner_executor" if branch == "workspace" else "single"),
                    completion_fn=forbidden_model,
                )
            if fault == "reset_error":
                assert current_task_lease(task_id) == leases[0]
                assert lease_row(task_id)["status"] == "released"
            else:
                assert current_task_lease() is None
            assert task_id not in runner._running_tasks
            assert task_id not in runner._lease_loss_requests
            assert task_id not in runner._shutdown_requests
            assert cancellation.task_token(task_id, create=False) is None
            released_token.assert_called_once_with(task_id)
            assert len(leases) == 1
            assert model_calls == []
            assert boundaries == ([] if branch == "unknown_context" else [branch])
            if branch == "workspace":
                finalized_root.assert_called_once_with(task_id)
            else:
                finalized_root.assert_not_called()
        finally:
            # RED probes also clean only their own synthetic ownership.
            if current_task_lease(task_id) is not None and bound_tokens:
                reset_task_lease(bound_tokens[0])
            for lease in leases:
                release_task_lease(lease)
            runner._running_tasks.pop(task_id, None)
            runner._lease_loss_requests.pop(task_id, None)
            runner._shutdown_requests.discard(task_id)
            cancellation.release_task_token(task_id)

    asyncio.run(exercise())
