import asyncio
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from app.artifacts.store import read_artifact, store_artifact
from app.runtime.cancellation import CancellationToken, cancel_task_token, release_task_token, task_token
from app.config import settings
from app.database import _scrub_privacy_sensitive_backup, connect, init_db, now_iso
from app.runtime import queue_service
from app.runtime.queue_service import cancel, claim, consume_steering_at_safe_point, enqueue, finish, pending_items, promote, recover_claimed_items
from app.tools.receipts import build_tool_receipt
from app.tools.registry import REGISTRY


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


def test_five_mid_task_instructions_are_consumed_in_stable_order() -> None:
    conversation_id = _conversation()
    task_id = _task(conversation_id)
    expected = [f"instruction-{index}" for index in range(1, 6)]
    for content in expected:
        enqueue(
            conversation_id=conversation_id,
            task_id=task_id,
            kind="steer",
            content=content,
            priority="next",
        )

    consumed = consume_steering_at_safe_point(task_id)

    assert [item.content for item in consumed] == expected
    assert pending_items(conversation_id=conversation_id) == []


@pytest.fixture
def fifo_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "queue-fifo.db"
    monkeypatch.setattr(settings, "database_path", str(path))
    init_db()
    return path


def _enqueue_same_time_steering(
    conversation_id: int, task_id: str, monkeypatch: pytest.MonkeyPatch
) -> list[queue_service.QueueItem]:
    # A clock collision must not let random UUID lexical order reorder input.
    identifiers = iter(uuid.UUID(int=value) for value in range(6, 0, -1))
    monkeypatch.setattr(queue_service, "now_iso", lambda: "2026-10-05T00:00:00.000000+00:00")
    monkeypatch.setattr(queue_service.uuid, "uuid4", lambda: next(identifiers))
    return [
        enqueue(
            conversation_id=conversation_id,
            task_id=task_id,
            kind="steer",
            content=f"instruction-{index}",
            priority="next",
        )
        for index in range(6)
    ]


@pytest.mark.parametrize("operation", ["pending", "consume"])
def test_same_time_queue_items_use_fifo_not_uuid_order(
    fifo_database: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    conversation_id = _conversation()
    task_id = _task(conversation_id)
    inserted = _enqueue_same_time_steering(conversation_id, task_id, monkeypatch)

    actual = (
        pending_items(conversation_id=conversation_id, kind="steer")
        if operation == "pending"
        else consume_steering_at_safe_point(task_id)
    )

    assert len({item.created_at for item in inserted}) == 1
    assert [item.id for item in actual] == [item.id for item in inserted]
    if operation == "consume":
        assert all(item.status == "consumed" for item in actual)
        assert pending_items(conversation_id=conversation_id, kind="steer") == []


def test_same_time_fifo_preserves_priority_cancel_restart_and_scrubbed_backup(
    fifo_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_id = _conversation()
    task_id = _task(conversation_id)
    with monkeypatch.context() as frozen:
        inserted = _enqueue_same_time_steering(conversation_id, task_id, frozen)
        cancel(inserted[1].id)
        promote(inserted[4].id, "now")
    expected = [inserted[index].id for index in (4, 0, 2, 3, 5)]

    # Startup and all reads open fresh connections: no in-memory order cache.
    init_db()
    assert [item.id for item in pending_items(conversation_id=conversation_id, kind="steer")] == expected
    backup_path = tmp_path / "queue-fifo-backup.db"
    with connect() as source, closing(sqlite3.connect(backup_path)) as destination:
        source.backup(destination)
    with closing(sqlite3.connect(backup_path)) as backup:
        # Leave a physical rowid gap, and exercise the product's real backup
        # scrub/VACUUM path using harmless, test-owned legacy model metadata.
        backup.execute("DELETE FROM conversation_queue_items WHERE id=?", (inserted[1].id,))
        backup.execute(
            "INSERT INTO stt_models(model_id,provider,status,storage_path,updated_at) VALUES(?,?,?,?,?)",
            ("fifo-test", "faster_whisper", "missing", str(tmp_path / "legacy-model"), now_iso()),
        )
        backup.commit()
    _scrub_privacy_sensitive_backup(backup_path)
    with closing(sqlite3.connect(backup_path)) as backup:
        assert backup.execute("SELECT storage_path FROM stt_models WHERE model_id='fifo-test'").fetchone()[0] == "managed:fifo-test"
        with connect() as destination:
            backup.backup(destination)
    init_db()

    assert [item.id for item in pending_items(conversation_id=conversation_id, kind="steer")] == expected
    assert [item.id for item in consume_steering_at_safe_point(task_id)] == expected
    assert pending_items(conversation_id=conversation_id, kind="steer") == []


def test_queue_claim_is_single_owner_and_live_claim_is_not_recovered() -> None:
    conversation_id = _conversation()
    task_id = _task(conversation_id)
    queued = enqueue(conversation_id=conversation_id, task_id=task_id, kind="submit", content="claim", priority="later")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: claim(queued.id), range(2)))

    winners = [item for item in outcomes if item is not None]
    assert len(winners) == 1
    assert winners[0].claim_owner_instance_id
    assert winners[0].claim_generation == 1
    assert recover_claimed_items() == 0
    assert finish(queued.id).status == "consumed"


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


def test_artifact_storage_is_content_addressed_and_deduplicated() -> None:
    conversation_id = _conversation()
    task_id = _task(conversation_id)
    first = store_artifact("same large result", task_id=task_id, tool_call_id="call-dedupe-1")
    second = store_artifact("same large result", task_id=task_id, tool_call_id="call-dedupe-2")

    assert first["artifact_id"] == second["artifact_id"]
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert read_artifact(first["artifact_id"])["content"] == "same large result"


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
