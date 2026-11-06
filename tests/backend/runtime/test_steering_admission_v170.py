import asyncio
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from app.api.routes.chat import remove_queue_item, steer_running_task
from app.config import settings
from app.context import service as context
from app.database import connect, init_db, now_iso, rows
from app.runtime import queue_service
from app.runtime.recovery import SteeringCheckpointConflict, create_checkpoint
from app.schemas import SteeringRequest


PROMPT = "Identical synthetic prompt and steering"


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "steering.sqlite3")
    init_db()


def seed_task(*, status="running"):
    stamp = now_iso()
    task_id = uuid.uuid4().hex
    with connect() as db:
        conversation_id = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("steering fixture", "", "ask", stamp, stamp),
        ).lastrowid
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, status, PROMPT, stamp, stamp),
        )
        message_id = db.execute(
            "INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
            (conversation_id, "user", PROMPT, task_id, stamp),
        ).lastrowid
    return task_id, conversation_id, message_id


def steer(task_id, **kwargs):
    return asyncio.run(steer_running_task(task_id, SteeringRequest(content=kwargs.pop("content", PROMPT), **kwargs)))


def state(task_id):
    return (
        rows("SELECT * FROM conversation_queue_items WHERE task_id=? ORDER BY rowid", (task_id,)),
        rows("SELECT id,content FROM messages WHERE task_id=? ORDER BY id", (task_id,)),
        rows("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)),
    )


def test_steering_admission_and_event_roll_back_together(monkeypatch):
    task_id, conversation_id, _ = seed_task()
    prior_state = state(task_id)
    prior_conversation = rows("SELECT updated_at FROM conversations WHERE id=?", (conversation_id,))
    emit = queue_service.emit_task_event

    def fail_after_inserting_event(*args, **kwargs):
        assert emit(*args, **kwargs)["id"]
        raise sqlite3.OperationalError("synthetic fault after event INSERT")

    request_id = uuid.uuid4()
    monkeypatch.setattr(queue_service, "emit_task_event", fail_after_inserting_event)
    with pytest.raises(sqlite3.OperationalError, match="synthetic fault"):
        steer(task_id, request_id=request_id)
    assert state(task_id) == prior_state
    assert rows("SELECT updated_at FROM conversations WHERE id=?", (conversation_id,)) == prior_conversation
    monkeypatch.setattr(queue_service, "emit_task_event", emit)
    assert steer(task_id, request_id=request_id)["id"] == request_id.hex
    queue, messages, events = state(task_id)
    assert len(queue) == 1 and len(messages) == 2 and len(events) == 1


def test_concurrent_retry_is_idempotent_and_rechecks_exact_identity():
    task_id, _, _ = seed_task()
    other_id, _, _ = seed_task()
    request_id = uuid.uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = [pool.submit(steer, task_id, request_id=request_id, priority="later") for _ in range(2)]
        assert [reply.result()["id"] for reply in replies] == [request_id.hex] * 2
    queue_service.promote(request_id.hex, "now")
    with connect() as db:
        db.execute("UPDATE agent_tasks SET status='completed' WHERE id=?", (task_id,))
    retry = steer(task_id, request_id=request_id, priority="later")
    assert retry["id"] == request_id.hex and retry["priority"] == "now"
    for target, options in [
        (other_id, {"priority": "later"}), (task_id, {"content": "other text", "priority": "later"}),
        (task_id, {"priority": "next"}),
    ]:
        with pytest.raises(HTTPException) as mismatch:
            steer(target, request_id=request_id, **options)
        assert mismatch.value.status_code == 409
        assert mismatch.value.detail["code"] == "steering_request_conflict"
    with pytest.raises(HTTPException) as terminal:
        steer(task_id, request_id=uuid.uuid4())
    assert terminal.value.status_code == 409
    assert terminal.value.detail["code"] == "steering_task_not_running"
    queue, messages, events = state(task_id)
    assert len(queue) == 1 and len(messages) == 2 and len(events) == 1
    assert state(other_id)[::2] == ([], [])


@pytest.mark.parametrize("scope,agent", [("agent", "child-1"), ("task", "child-1")])
def test_unsupported_routing_does_not_apply_to_root(scope, agent):
    task_id, _, _ = seed_task()
    before = state(task_id)
    with pytest.raises(HTTPException) as rejected:
        steer(task_id, request_id=uuid.uuid4(), target_scope=scope, target_agent_id=agent)
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "unsupported_steering_scope"
    assert state(task_id) == before


def test_cancel_exact_duplicate_preserves_original_and_worker_fences_checkpoint():
    task_id, conversation_id, original = seed_task()
    request_id = uuid.uuid4()
    first = steer(task_id, request_id=request_id)
    second = steer(task_id)
    first_message = first["payload"][queue_service.STEERING_MESSAGE_ID]
    second_message = second["payload"][queue_service.STEERING_MESSAGE_ID]
    stale_worker_selection = tuple(item.id for item in queue_service.pending_steering(task_id))
    assert stale_worker_selection == (first["id"], second["id"])
    assert [entry["content"] for entry in context.model_history(conversation_id)] == [PROMPT]
    removed = asyncio.run(remove_queue_item(first["id"]))
    assert removed["status"] == "cancelled"
    assert [row["id"] for row in state(task_id)[1]] == [original, second_message]
    assert first_message not in {row["id"] for row in state(task_id)[1]}
    with pytest.raises(SteeringCheckpointConflict):
        create_checkpoint(task_id, "", "conversation", "steering_applied", {}, steering_item_ids=stale_worker_selection)
    assert queue_service.get_item(second["id"]).status == "pending"
    assert steer(task_id, request_id=request_id)["status"] == "cancelled"
    assert len(state(task_id)[1]) == 2 and len(state(task_id)[2]) == 2


def test_cancel_mirror_failure_rolls_back_queue_status():
    task_id, _, _ = seed_task()
    item = steer(task_id)
    message_id = item["payload"][queue_service.STEERING_MESSAGE_ID]
    with connect() as db:
        db.execute(f"CREATE TRIGGER simulate_mirror_delete_failure BEFORE DELETE ON messages WHEN OLD.id={message_id} "
                   "BEGIN SELECT RAISE(ABORT,'synthetic mirror delete failure'); END")
    before = state(task_id)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic mirror delete failure"):
        asyncio.run(remove_queue_item(item["id"]))
    assert state(task_id) == before


def test_legacy_cancel_preserves_database_rows_but_fences_model_and_compaction(monkeypatch):
    task_id, conversation_id, original = seed_task()
    legacy = queue_service.enqueue(conversation_id=conversation_id, task_id=task_id, kind="steer", content=PROMPT)
    with connect() as db:
        mirror_id = db.execute("INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
                               (conversation_id, "user", PROMPT, task_id, now_iso())).lastrowid
    current = steer(task_id)
    queue_service.finish(current["id"])
    asyncio.run(remove_queue_item(legacy.id))
    assert [row["id"] for row in state(task_id)[1]] == [original, mirror_id, current["payload"][queue_service.STEERING_MESSAGE_ID]]
    assert [item["content"] for item in context.model_history(conversation_id)] == [PROMPT, PROMPT]
    assert [item["content"] for item in context.model_history(conversation_id, exclude_steering_for_task_id=task_id)] == [PROMPT]
    with connect() as db:
        db.execute("INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
                   (conversation_id, "assistant", "synthetic reply", task_id, now_iso()))
    received = []

    async def fake_completion(prompt, *args, **kwargs):
        received.extend(prompt)
        return {"content": '{"goal":"synthetic"}'}

    monkeypatch.setattr(context, "KEEP_RECENT_MESSAGES", 1)
    monkeypatch.setattr(context, "completion", fake_completion)
    assert asyncio.run(context.compact_conversation(conversation_id, None, force=True))["compacted"]
    assert received[1]["content"].count(PROMPT) == 2  # Original + exact applied mirror, never cancelled legacy copy.
