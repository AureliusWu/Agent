from __future__ import annotations

import uuid
import time

import pytest

from app.database import connect, init_db, now_iso
from app.kernel.adapters import SqliteTaskStore
from app.runtime.task_state import (
    InvalidTaskTransition,
    TaskStatus,
    can_transition,
    transition_task,
)
from app.tools.receipts import build_tool_receipt


def _task() -> tuple[int, str]:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "v12 state", "", "full", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, TaskStatus.RUNNING.value, "state", stamp, stamp),
        )
    return conversation_id, task_id


def test_v12_state_machine_rejects_illegal_and_final_reentry() -> None:
    assert can_transition(TaskStatus.PENDING, TaskStatus.RUNNING)
    assert can_transition(TaskStatus.RUNNING, TaskStatus.VERIFYING)
    assert not can_transition(TaskStatus.RUNNING, TaskStatus.COMPLETED)
    assert can_transition(TaskStatus.VERIFYING, TaskStatus.COMPLETED, verifier=True)
    assert not can_transition(TaskStatus.CANCELLED, TaskStatus.RUNNING)


def test_transition_task_persists_reason_step_and_source() -> None:
    _, task_id = _task()
    with connect() as db:
        transition_task(
            db,
            task_id=task_id,
            target=TaskStatus.WAITING_TOOL,
            assignments={"current_step": "tool:write_file"},
            reason="tool_started",
            trigger_source="test.runtime",
        )
        row = db.execute(
            "SELECT from_status,to_status,reason,current_step,trigger_source FROM task_transitions WHERE task_id=?",
            (task_id,),
        ).fetchone()
    assert tuple(row) == (
        TaskStatus.RUNNING.value,
        TaskStatus.WAITING_TOOL.value,
        "tool_started",
        "tool:write_file",
        "test.runtime",
    )


def test_final_task_cannot_be_reopened() -> None:
    _, task_id = _task()
    with connect() as db:
        transition_task(
            db,
            task_id=task_id,
            target=TaskStatus.COMPLETED,
            verifier=True,
            trigger_source="test.verifier",
        )
        with pytest.raises(InvalidTaskTransition):
            transition_task(
                db,
                task_id=task_id,
                target=TaskStatus.RUNNING,
                trigger_source="test.runtime",
            )


def test_task_store_records_runtime_transitions() -> None:
    _, task_id = _task()
    store = SqliteTaskStore()
    store.update_task(task_id, TaskStatus.INTERRUPTED, current_step="safe_point")
    with connect() as db:
        row = db.execute(
            "SELECT from_status,to_status,current_step FROM task_transitions WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    assert tuple(row) == ("running", "interrupted", "safe_point")


def test_v12_tool_receipt_is_redacted_and_persisted() -> None:
    conversation_id, task_id = _task()
    stamp = now_iso()
    receipt = build_tool_receipt(
        "write_file",
        {
            "success": True,
            "status": "ok",
            "data": {"path": "safe.txt", "change_id": "change-1"},
            "metadata": {"duration_ms": 12},
        },
        task_id=task_id,
        tool_call_id="call-1",
        arguments={"path": "safe.txt", "api_key": "secret-value"},
        permission_decision="approved",
        risk_level="medium",
        started_at=stamp,
    )
    result = {"success": True, "status": "ok", "receipt": receipt.as_dict()}
    SqliteTaskStore().record_tool_run(
        conversation_id=conversation_id,
        task_id=task_id,
        tool="write_file",
        arguments={"path": "safe.txt"},
        result=result,
        started=stamp,
        started_perf=time.perf_counter(),
        risk="medium",
        confirmed=True,
        execution_id="execution-1",
    )

    with connect() as db:
        row = db.execute(
            "SELECT status,receipt_json FROM tool_receipts WHERE receipt_id=?",
            (receipt.receipt_id,),
        ).fetchone()
    assert row["status"] == "SUCCESS"
    assert "secret-value" not in row["receipt_json"]
    assert receipt.rollback["change_id"] == "change-1"
