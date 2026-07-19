import asyncio
import uuid

import pytest

from app.artifact_store import read_artifact, store_artifact
from app.cancellation import CancellationToken, cancel_task_token, release_task_token, task_token
from app.database import connect, now_iso
from app.queue_service import cancel, consume_steering_at_safe_point, enqueue, pending_items, promote
from app.tool_receipts import build_tool_receipt
from app.tool_registry import REGISTRY


def _conversation() -> int:
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("runtime-v3", "", "ask", stamp, stamp),
        )
        return int(cursor.lastrowid)


def _task(conversation_id: int) -> str:
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "pending", "v3", stamp, stamp),
        )
    return task_id


def test_persistent_queue_orders_promotes_consumes_and_cancels() -> None:
    conversation_id = _conversation()
    task_id = _task(conversation_id)
    later = enqueue(conversation_id=conversation_id, task_id=task_id, kind="submit", content="later", priority="later")
    next_item = enqueue(conversation_id=conversation_id, task_id=task_id, kind="steer", content="next", priority="next")

    assert [item.id for item in pending_items(conversation_id=conversation_id)] == [next_item.id, later.id]
    assert promote(later.id, "now").priority == "now"
    assert [item.id for item in pending_items(conversation_id=conversation_id)][0] == later.id
    assert consume_steering_at_safe_point(task_id)[0].content == "next"
    assert cancel(later.id).status == "cancelled"
    assert pending_items(conversation_id=conversation_id) == []


def test_cancellation_token_propagates_reason_to_children() -> None:
    root = CancellationToken(scope="task", identifier="root")
    model = root.child("model", "model-1")
    tool = model.child("tool", "tool-1")

    root.cancel("administrator_stop")

    assert root.cancelled and model.cancelled and tool.cancelled
    assert asyncio.run(tool.wait()) == "administrator_stop"
    with pytest.raises(asyncio.CancelledError):
        tool.raise_if_cancelled()


def test_task_token_registry_cancels_and_releases() -> None:
    token = task_token("registered")
    assert token is not None
    assert cancel_task_token("registered", "stop") is True
    assert token.cancelled is True
    release_task_token("registered")
    assert task_token("registered", create=False) is None


def test_tool_contract_receipt_and_artifact_incremental_read() -> None:
    conversation_id = _conversation()
    task_id = _task(conversation_id)
    stored = store_artifact("abcdefghij", task_id=task_id, tool_call_id="call-1")
    first = read_artifact(stored["artifact_id"], offset=0, limit=4)
    second = read_artifact(stored["artifact_id"], offset=first["next_offset"], limit=20)
    receipt = build_tool_receipt(
        "run_command",
        {"success": True, "status": "ok", "data": {"exit_code": 0, **stored}, "truncated": True},
    )

    assert first["content"] == "abcd" and first["truncated"] is True
    assert second["content"] == "efghij" and second["truncated"] is False
    assert receipt.exit_code == 0 and receipt.artifact_id == stored["artifact_id"]
    assert receipt.receipt_version == 2 and receipt.operation_kind == "mutation"
    assert receipt.error_fingerprint is None
    assert REGISTRY["read_file"].interruptibility == "cancel"
    assert REGISTRY["write_file"].interruptibility == "block"
    assert REGISTRY["run_command"].concurrency_policy == "exclusive"


def test_tool_receipt_v2_distinguishes_reads_mutations_and_stable_failures() -> None:
    read = build_tool_receipt("read_file", {"success": True, "data": {"path": "a.txt"}})
    mutation = build_tool_receipt(
        "write_file",
        {
            "success": True,
            "data": {
                "path": "a.txt",
                "version_before": "file:1:before",
                "version_after": "file:1:after",
                "change_id": "1-aaaaaaaa",
            },
        },
    )
    first = build_tool_receipt("write_file", {"success": False, "status": "error", "error_code": "version_conflict", "retryable": True})
    second = build_tool_receipt("write_file", {"success": False, "status": "error", "error_code": "version_conflict", "retryable": True, "error_message": "different private detail"})
    assert read.operation_kind == "read" and read.observed_files == ("a.txt",) and not read.changed_files
    assert mutation.operation_kind == "mutation" and mutation.changed_files == ("a.txt",)
    assert mutation.version_before == "file:1:before" and mutation.version_after == "file:1:after"
    assert first.error_fingerprint == second.error_fingerprint
