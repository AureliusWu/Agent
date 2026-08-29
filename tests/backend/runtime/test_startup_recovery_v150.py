from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from app.database import connect, init_db, now_iso
from app.runtime.recovery import create_checkpoint, operation_execution_id, prepare_operation
from app.runtime.runner import run_chat
from app.runtime.task_leases import acquire_task_lease, release_task_lease
from app.runtime.task_runtime import list_tasks
from app.runtime.task_state import (
    ACTIVE_TASK_STATUSES,
    RESUMABLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    TaskStatus,
    can_transition,
)
from app.schemas import ChatRequest


def _conversation(workspace: Path, *, permission_mode: str = "ask") -> int:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "v15 startup recovery", str(workspace), permission_mode, stamp, stamp),
        )
    return conversation_id


def _task(conversation_id: int, status: TaskStatus, *, current_step: str | None = None) -> str:
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,current_step,created_at,updated_at,started_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, conversation_id, status.value, f"recover {status.value}", current_step, stamp, stamp, stamp),
        )
    return task_id


def _cleanup_conversation(conversation_id: int) -> None:
    with connect() as db:
        db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))


def test_v15_task_status_sets_are_disjoint_complete_and_authoritative() -> None:
    assert ACTIVE_TASK_STATUSES.isdisjoint(RESUMABLE_TASK_STATUSES)
    assert ACTIVE_TASK_STATUSES.isdisjoint(TERMINAL_TASK_STATUSES)
    assert RESUMABLE_TASK_STATUSES.isdisjoint(TERMINAL_TASK_STATUSES)
    assert ACTIVE_TASK_STATUSES | RESUMABLE_TASK_STATUSES | TERMINAL_TASK_STATUSES == frozenset(TaskStatus)

    assert {
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_TOOL,
        TaskStatus.VERIFYING,
        TaskStatus.REPAIRING,
    } <= ACTIVE_TASK_STATUSES
    assert {
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.WAITING_PROVIDER,
        TaskStatus.WAITING_PROVIDER_CREDENTIAL,
        TaskStatus.INTERRUPTED,
        TaskStatus.TIMED_OUT,
    } <= RESUMABLE_TASK_STATUSES
    assert {
        TaskStatus.COMPLETED,
        TaskStatus.PARTIALLY_COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    } == TERMINAL_TASK_STATUSES
    for status in ACTIVE_TASK_STATUSES - {TaskStatus.CANCEL_REQUESTED}:
        assert can_transition(status, TaskStatus.INTERRUPTED)
    assert not can_transition(TaskStatus.CANCEL_REQUESTED, TaskStatus.COMPLETED, verifier=True)


def test_v15_active_task_query_reuses_the_authoritative_active_set(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path)
    try:
        task_ids = {status: _task(conversation_id, status) for status in TaskStatus}
        returned = {item["id"] for item in list_tasks(conversation_id, active_only=True)}
        assert returned == {task_ids[status] for status in ACTIVE_TASK_STATUSES}
    finally:
        _cleanup_conversation(conversation_id)


def test_v15_startup_recovery_applies_explicit_policy_to_every_task_state(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path)
    expected = {
        status: (
            TaskStatus.PENDING
            if status == TaskStatus.PENDING
            else TaskStatus.CANCELLED
            if status == TaskStatus.CANCEL_REQUESTED
            else TaskStatus.INTERRUPTED
            if status in ACTIVE_TASK_STATUSES
            else status
        )
        for status in TaskStatus
    }
    task_ids = {status: _task(conversation_id, status, current_step=status.value) for status in expected}
    try:
        init_db()
        with connect() as db:
            recovered = {
                TaskStatus(str(row["original_status"])): (TaskStatus(str(row["status"])), int(row["resumable"]))
                for row in db.execute(
                    "SELECT t.status,t.resumable,substr(t.prompt,9) AS original_status "
                    "FROM agent_tasks t WHERE t.conversation_id=?",
                    (conversation_id,),
                ).fetchall()
            }
            transitions = {
                str(row["task_id"]): (str(row["from_status"]), str(row["to_status"]))
                for row in db.execute(
                    "SELECT task_id,from_status,to_status FROM task_transitions WHERE task_id IN "
                    f"({','.join('?' for _ in task_ids)})",
                    tuple(task_ids.values()),
                ).fetchall()
            }

        for original, target in expected.items():
            assert recovered[original][0] == target
        assert recovered[TaskStatus.PENDING][1] == 1
        for waiting in RESUMABLE_TASK_STATUSES:
            assert recovered[waiting][1] == 1
        for terminal in TERMINAL_TASK_STATUSES:
            assert recovered[terminal][1] == 0
        for interrupted in ACTIVE_TASK_STATUSES - {TaskStatus.PENDING, TaskStatus.CANCEL_REQUESTED}:
            assert transitions[task_ids[interrupted]] == (interrupted.value, TaskStatus.INTERRUPTED.value)
        assert transitions[task_ids[TaskStatus.CANCEL_REQUESTED]] == (
            TaskStatus.CANCEL_REQUESTED.value,
            TaskStatus.CANCELLED.value,
        )
    finally:
        _cleanup_conversation(conversation_id)


def test_v15_startup_preserves_an_active_state_while_its_lease_is_live(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path)
    task_id = _task(conversation_id, TaskStatus.WAITING_TOOL, current_step="tool:read_file")
    lease = acquire_task_lease(task_id, ttl_seconds=30)
    try:
        init_db()
        with connect() as db:
            assert db.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0] == "waiting_tool"
        assert release_task_lease(lease) is True
        init_db()
        with connect() as db:
            assert db.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0] == "interrupted"
    finally:
        release_task_lease(lease)
        _cleanup_conversation(conversation_id)


def test_v15_model_call_crash_gets_a_safe_recovery_checkpoint(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path)
    task_id = _task(conversation_id, TaskStatus.RUNNING, current_step="model_round_1")
    try:
        init_db()
        with connect() as db:
            task = dict(db.execute("SELECT status,resumable,checkpoint_sequence FROM agent_tasks WHERE id=?", (task_id,)).fetchone())
            checkpoint = dict(
                db.execute(
                    "SELECT sequence,phase,reason,state,workspace_hash FROM task_checkpoints WHERE task_id=?",
                    (task_id,),
                ).fetchone()
            )
        assert task == {"status": "interrupted", "resumable": 1, "checkpoint_sequence": 1}
        assert checkpoint["sequence"] == 1
        assert checkpoint["reason"] == "startup_recovery_baseline"
        assert checkpoint["workspace_hash"]
        assert json.loads(checkpoint["state"])["goal"] == "recover running"

        async def recovered_completion(messages, api_key=None, **kwargs):
            return {"role": "assistant", "content": "recovered after model crash"}

        resumed = asyncio.run(
            run_chat(
                ChatRequest(
                    conversation_id=conversation_id,
                    content="recover running",
                    task_id=task_id,
                    resume=True,
                ),
                completion_fn=recovered_completion,
            )
        )
        assert resumed["task_status"] == "completed"
    finally:
        _cleanup_conversation(conversation_id)


def test_v15_startup_reconciles_receipts_and_marks_unknown_side_effects_uncertain(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path)
    receipt_task = _task(conversation_id, TaskStatus.WAITING_TOOL, current_step="tool:write_file")
    uncertain_task = _task(conversation_id, TaskStatus.WAITING_TOOL, current_step="tool:run_command")
    receipt_checkpoint = create_checkpoint(receipt_task, str(tmp_path), "implementation", "before_side_effect", {})
    uncertain_checkpoint = create_checkpoint(uncertain_task, str(tmp_path), "implementation", "before_side_effect", {})
    receipt = {
        "receipt_id": uuid.uuid4().hex,
        "task_id": receipt_task,
        "tool_call_id": "write-once",
        "tool": "write_file",
        "success": True,
        "status": "ok",
        "standard_status": "SUCCESS",
    }
    receipt_call = {
        "id": "write-once",
        "type": "function",
        "function": {"name": "write_file", "arguments": json.dumps({"path": "note.txt", "content": "once"})},
    }
    uncertain_call = {
        "id": "command-once",
        "type": "function",
        "function": {"name": "run_command", "arguments": json.dumps({"command": "python", "args": ["--version"]})},
    }
    receipt_execution_id = operation_execution_id(receipt_task, receipt_call)
    uncertain_execution_id = operation_execution_id(uncertain_task, uncertain_call)
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO task_operations("
            "execution_id,task_id,checkpoint_sequence,tool_call_id,tool,arguments_hash,status,side_effect,started_at"
            ") "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                receipt_execution_id,
                receipt_task,
                receipt_checkpoint["sequence"],
                "write-once",
                "write_file",
                "hash-1",
                "running",
                1,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO task_operations("
            "execution_id,task_id,checkpoint_sequence,tool_call_id,tool,arguments_hash,status,side_effect,started_at"
            ") "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                uncertain_execution_id,
                uncertain_task,
                uncertain_checkpoint["sequence"],
                "command-once",
                "run_command",
                "hash-2",
                "running",
                1,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO tool_receipts("
            "receipt_id,task_id,tool_call_id,tool_name,status,receipt_json,created_at"
            ") VALUES(?,?,?,?,?,?,?)",
            (receipt["receipt_id"], receipt_task, "write-once", "write_file", "SUCCESS", json.dumps(receipt), stamp),
        )

    try:
        init_db()
        with connect() as db:
            completed = dict(
                db.execute(
                    "SELECT status,result FROM task_operations WHERE execution_id=?",
                    (receipt_execution_id,),
                ).fetchone()
            )
            uncertain = dict(
                db.execute(
                    "SELECT status,result FROM task_operations WHERE execution_id=?",
                    (uncertain_execution_id,),
                ).fetchone()
            )
            task_states = {
                str(row["id"]): str(row["status"])
                for row in db.execute(
                    "SELECT id,status FROM agent_tasks WHERE id IN (?,?)",
                    (receipt_task, uncertain_task),
                ).fetchall()
            }
        assert completed["status"] == "completed"
        assert json.loads(completed["result"])["receipt"]["receipt_id"] == receipt["receipt_id"]
        assert uncertain["status"] == "uncertain"
        assert json.loads(uncertain["result"])["error_code"] == "crash_recovery_unknown_side_effect"
        assert task_states == {receipt_task: "interrupted", uncertain_task: "interrupted"}
        reused = prepare_operation(
            receipt_task,
            receipt_checkpoint["sequence"],
            receipt_call,
            {"path": "note.txt", "content": "once"},
            side_effect=True,
        )
        refused_replay = prepare_operation(
            uncertain_task,
            uncertain_checkpoint["sequence"],
            uncertain_call,
            {"command": "python", "args": ["--version"]},
            side_effect=True,
        )
        assert reused["created"] is False and reused["status"] == "completed"
        assert refused_replay["created"] is False and refused_replay["status"] == "uncertain"
    finally:
        _cleanup_conversation(conversation_id)


def test_v15_init_db_preserves_readonly_permission_across_restart(tmp_path: Path) -> None:
    readonly_id = _conversation(tmp_path, permission_mode="readonly")
    legacy_confirm_id = _conversation(tmp_path, permission_mode="confirm")
    try:
        init_db()
        with connect() as db:
            modes = {
                int(row["id"]): str(row["permission_mode"])
                for row in db.execute("SELECT id,permission_mode FROM conversations WHERE id IN (?,?)", (readonly_id, legacy_confirm_id))
            }
        assert modes[readonly_id] == "readonly"
        assert modes[legacy_confirm_id] == "ask"
        with connect() as db:
            versions = {int(row[0]) for row in db.execute("SELECT version FROM schema_migrations")}
            assert set(range(1, 43)) <= versions
    finally:
        _cleanup_conversation(readonly_id)
        _cleanup_conversation(legacy_confirm_id)
