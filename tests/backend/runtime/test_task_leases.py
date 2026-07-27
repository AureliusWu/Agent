import asyncio
import multiprocessing
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.database import connect, init_db, now_iso
from app.config import settings
from app.kernel.adapters import SqliteTaskStore
from app.runtime.recovery import create_checkpoint, prepare_operation, set_operation_status
from app.runtime.runner import run_chat
from app.runtime.task_events import emit_task_event
from app.runtime.task_leases import (
    TaskLeaseConflict,
    acquire_task_lease,
    bind_task_lease,
    release_task_lease,
    renew_task_lease,
    reset_task_lease,
)
from app.runtime.task_state import TaskStatus
from app.runtime.verification import finalize_task_from_verification
from app.schemas import ChatRequest


def _resume_process(database_path: str, conversation_id: int, task_id: str, start_event, result_queue, execution_log: str) -> None:
    from app.config import settings as process_settings

    process_settings.database_path = Path(database_path)

    async def completion(*args, **kwargs):
        with Path(execution_log).open("a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        await asyncio.sleep(1)
        return {"role": "assistant", "content": "single owner completed"}

    start_event.wait(10)
    try:
        result = asyncio.run(run_chat(
            ChatRequest(conversation_id=conversation_id, content="resume race", task_id=task_id),
            completion_fn=completion,
            precreated=True,
        ))
        result_queue.put({"kind": "result", "status": result.get("task_status"), "lease_conflict": result.get("lease_conflict", False)})
    except Exception as exc:
        result_queue.put({"kind": "error", "type": type(exc).__name__, "message": str(exc)})


def _running_task() -> str:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "lease", "", "ask", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "lease test", stamp, stamp),
        )
    return task_id


def test_task_lease_is_exclusive_and_owner_can_renew() -> None:
    task_id = _running_task()
    lease = acquire_task_lease(task_id, ttl_seconds=20)
    with pytest.raises(TaskLeaseConflict):
        acquire_task_lease(task_id, ttl_seconds=20)

    renewed = renew_task_lease(lease, ttl_seconds=40)
    assert renewed.expires_at > lease.expires_at
    assert release_task_lease(renewed) is True
    replacement = acquire_task_lease(task_id, ttl_seconds=20)
    assert replacement.generation == lease.generation + 1
    assert release_task_lease(replacement) is True


def test_init_db_preserves_running_task_with_live_lease() -> None:
    task_id = _running_task()
    lease = acquire_task_lease(task_id, ttl_seconds=20)
    init_db()
    with connect() as db:
        status = db.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0]
    assert status == "running"
    release_task_lease(lease)


def test_init_db_interrupts_task_owned_by_dead_process() -> None:
    task_id = _running_task()
    lease = acquire_task_lease(task_id, ttl_seconds=20)
    with connect() as db:
        db.execute(
            "UPDATE task_leases SET owner_pid=?,expires_at=? WHERE task_id=?",
            (2_147_483_647, time.time() + 60, task_id),
        )
    init_db()
    with connect() as db:
        task_status = db.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0]
        lease_status = db.execute("SELECT status FROM task_leases WHERE task_id=?", (task_id,)).fetchone()[0]
    assert task_status == "interrupted"
    assert lease_status == "expired"
    assert release_task_lease(lease) is False


def test_expired_task_lease_is_taken_over_with_new_generation() -> None:
    task_id = _running_task()
    lease = acquire_task_lease(task_id, ttl_seconds=20)
    with connect() as db:
        db.execute(
            "UPDATE task_leases SET expires_at=? WHERE task_id=?",
            (time.time() - 1, task_id),
        )
    replacement = acquire_task_lease(task_id, ttl_seconds=20)
    assert replacement.generation == lease.generation + 1
    assert replacement.owner_instance_id == lease.owner_instance_id
    assert release_task_lease(replacement) is True


def test_concurrent_claim_allows_exactly_one_task_lease_owner() -> None:
    task_id = _running_task()

    def compete() -> object:
        try:
            return acquire_task_lease(task_id, ttl_seconds=20)
        except TaskLeaseConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: compete(), range(2)))

    winners = [item for item in outcomes if not isinstance(item, TaskLeaseConflict)]
    conflicts = [item for item in outcomes if isinstance(item, TaskLeaseConflict)]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert release_task_lease(winners[0]) is True


def test_two_process_resume_executes_model_once(tmp_path) -> None:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "process resume", "", "ask", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "pending", "resume race", stamp, stamp),
        )
    execution_log = tmp_path / "model-executions.txt"
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_resume_process,
            args=(str(settings.database_path), conversation_id, task_id, start_event, result_queue, str(execution_log)),
        )
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    outcomes = [result_queue.get(timeout=5) for _process in processes]
    executions = execution_log.read_text(encoding="utf-8").splitlines()
    with connect() as db:
        final_status = db.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0]
    assert len(executions) == 1
    assert final_status == "completed"
    assert sum(item.get("status") == "completed" for item in outcomes) == 1
    assert any(item.get("lease_conflict") or item.get("kind") == "error" for item in outcomes)


def test_conflicting_precreated_run_does_not_overwrite_task_status() -> None:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "lease conflict", "", "ask", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "pending", "lease conflict", stamp, stamp),
        )
    holder = acquire_task_lease(task_id, ttl_seconds=20)

    with pytest.raises(HTTPException) as conflict:
        asyncio.run(run_chat(
            ChatRequest(conversation_id=conversation_id, content="lease conflict", task_id=task_id),
            precreated=True,
        ))

    with connect() as db:
        task = dict(db.execute("SELECT status,current_step,termination_reason FROM agent_tasks WHERE id=?", (task_id,)).fetchone())
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "owned_by_other_runtime"
    assert task == {"status": "pending", "current_step": None, "termination_reason": None}
    assert release_task_lease(holder) is True


def test_stale_generation_cannot_write_task_operation_or_terminal_event(tmp_path) -> None:
    task_id = _running_task()
    first = acquire_task_lease(task_id, ttl_seconds=20)
    context_token = bind_task_lease(first)
    call = {
        "id": "mutating-call",
        "function": {"name": "write_file", "arguments": '{"path":"x.txt","content":"x"}'},
    }
    checkpoint = create_checkpoint(task_id, str(tmp_path), "analysis", "lease-ledger", {})
    operation = prepare_operation(task_id, 0, call, {"path": "x.txt", "content": "x"}, side_effect=True)
    with connect() as db:
        conversation_id = int(db.execute("SELECT conversation_id FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0])
    SqliteTaskStore().record_tool_run(
        conversation_id=conversation_id,
        task_id=task_id,
        tool="write_file",
        arguments={"path": "x.txt"},
        result={"success": True, "status": "ok"},
        started=now_iso(),
        started_perf=time.perf_counter(),
        risk="high",
        confirmed=True,
        execution_id="lease-ledger-tool-run",
    )
    try:
        with connect() as db:
            db.execute("UPDATE task_leases SET expires_at=? WHERE task_id=?", (time.time() - 1, task_id))
        replacement = acquire_task_lease(task_id, ttl_seconds=20)

        with pytest.raises(TaskLeaseConflict):
            SqliteTaskStore().update_task(task_id, TaskStatus.INTERRUPTED, current_step="stale")
        with pytest.raises(TaskLeaseConflict):
            set_operation_status(operation["execution_id"], "completed", {"success": True})
        with pytest.raises(TaskLeaseConflict):
            finalize_task_from_verification(task_id, {"status": "passed", "reason": "stale"})
        event = emit_task_event(task_id, "task.completed", {"status": "completed"})

        with connect() as db:
            task = dict(db.execute("SELECT status,current_step,lease_generation FROM agent_tasks WHERE id=?", (task_id,)).fetchone())
            stored_operation = dict(db.execute("SELECT status,lease_generation FROM task_operations WHERE execution_id=?", (operation["execution_id"],)).fetchone())
            terminal_events = db.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND event_type='task.completed'", (task_id,)).fetchone()[0]
            checkpoint_generation = db.execute("SELECT lease_generation FROM task_checkpoints WHERE id=?", (checkpoint["id"],)).fetchone()[0]
            tool_generation = db.execute("SELECT lease_generation FROM tool_runs WHERE execution_id='lease-ledger-tool-run'").fetchone()[0]
        assert task == {"status": "running", "current_step": None, "lease_generation": replacement.generation}
        assert stored_operation == {"status": "running", "lease_generation": first.generation}
        assert event["suppressed"] is True
        assert terminal_events == 0
        assert checkpoint_generation == first.generation
        assert tool_generation == first.generation
    finally:
        reset_task_lease(context_token)
        release_task_lease(replacement)
