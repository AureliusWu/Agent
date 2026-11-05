from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import connect, init_db, now_iso, rows
from app.main import app
from app.providers.configuration import ProviderConfiguration, save_provider_configuration
from app.runtime import task_runtime
from app.voice.events import voice_events


_INTAKE_TABLES = ("agent_tasks", "messages", "conversation_queue_items", "task_transitions", "task_events")


@pytest.fixture(autouse=True)
def isolated_intake_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "database_path", tmp_path / "task-intake.db")
    monkeypatch.setattr(settings, "log_path", tmp_path / "task-intake.log")
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider-settings.json"))
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "tavily_api_key", "")
    monkeypatch.setattr(settings, "brave_api_key", "")
    monkeypatch.setattr(task_runtime, "_scheduled_task_ids", set())
    monkeypatch.setattr(task_runtime, "_ephemeral_credentials", {})
    save_provider_configuration(ProviderConfiguration(provider_id="mock"))
    init_db()


def _conversation() -> int:
    stamp = now_iso()
    with connect() as db:
        return int(db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("intake", "", "ask", stamp, stamp),
        ).lastrowid)


def _intake_counts(task_id: str) -> dict[str, int]:
    with connect() as db:
        return {
            table: db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {'id' if table == 'agent_tasks' else 'task_id'}=?",
                (task_id,),
            ).fetchone()[0]
            for table in _INTAKE_TABLES
        }


@pytest.mark.parametrize("failing_table", ["conversation_queue_items", "task_events"])
def test_intake_sql_failure_rolls_back_every_record_and_voice_binding(failing_table: str, monkeypatch) -> None:
    """DSH-NEXT-INTAKE-01: failure before commit leaves no half-admitted work."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    monkeypatch.setattr(task_runtime, "_queue", queue)
    conversation_id = _conversation()
    task_id = uuid.uuid4().hex
    voice_session_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        before = db.execute("SELECT updated_at FROM conversations WHERE id=?", (conversation_id,)).fetchone()[0]
        db.execute(
            "INSERT INTO voice_sessions(voice_session_id,conversation_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
            (voice_session_id, conversation_id, "REVIEWING", stamp, stamp),
        )
        db.execute(
            f"CREATE TRIGGER intake_failure BEFORE INSERT ON {failing_table} "
            "BEGIN SELECT RAISE(ABORT, 'injected intake SQL failure'); END"
        )

    # The queue is test-owned; avoid lifespan startup and model execution.
    response = TestClient(app).post("/api/tasks", json={
        "conversation_id": conversation_id, "task_id": task_id,
        "voice_session_id": voice_session_id, "content": "synthetic voice input",
    })

    assert response.status_code == 503
    assert _intake_counts(task_id) == dict.fromkeys(_INTAKE_TABLES, 0)
    assert rows("SELECT updated_at FROM conversations WHERE id=?", (conversation_id,)) == [{"updated_at": before}]
    assert rows("SELECT state,task_id,message_id,updated_at FROM voice_sessions WHERE voice_session_id=?", (voice_session_id,)) == [
        {"state": "REVIEWING", "task_id": None, "message_id": None, "updated_at": stamp},
    ]
    assert voice_events(voice_session_id) == []
    assert queue.empty()
    assert task_id not in task_runtime._scheduled_task_ids
    assert task_id not in task_runtime._ephemeral_credentials


def test_intake_commit_precedes_wakeup_and_duplicate_id_cannot_submit_again(monkeypatch) -> None:
    """DSH-NEXT-INTAKE-02: one intake, one wake, duplicate ID stays HTTP 409."""
    conversation_id = _conversation()
    another_conversation = _conversation()
    task_id = uuid.uuid4().hex
    committed_at_wakeup: list[dict[str, int]] = []

    class CheckingQueue(asyncio.Queue[str]):
        def put_nowait(self, item: str) -> None:
            committed_at_wakeup.append(_intake_counts(task_id))
            super().put_nowait(item)

    queue = CheckingQueue()
    monkeypatch.setattr(task_runtime, "_queue", queue)
    payload = {"conversation_id": conversation_id, "task_id": task_id, "content": "original request"}
    client = TestClient(app)
    submitted = client.post("/api/tasks", json=payload)
    duplicate = client.post("/api/tasks", json=payload)
    wrong_conversation = client.post(
        "/api/tasks", json={**payload, "conversation_id": another_conversation, "content": "different request"},
    )

    assert submitted.status_code == 202
    assert duplicate.status_code == 409
    assert wrong_conversation.status_code == 409
    assert queue.get_nowait() == submitted.json()["queue_item"]["id"]
    assert queue.empty()
    assert committed_at_wakeup == [dict.fromkeys(_INTAKE_TABLES, 1)]
    assert _intake_counts(task_id) == dict.fromkeys(_INTAKE_TABLES, 1)
    assert rows("SELECT conversation_id,content FROM messages WHERE task_id=?", (task_id,)) == [
        {"conversation_id": conversation_id, "content": "original request"},
    ]
    stored_payload = rows("SELECT payload_json FROM conversation_queue_items WHERE task_id=?", (task_id,))[0]["payload_json"]
    assert json.loads(stored_payload)["conversation_id"] == conversation_id


def test_voice_notification_failure_does_not_undo_intake_or_keep_microphone(monkeypatch) -> None:
    """DSH-NEXT-INTAKE-04: postcommit notifications do not own admission."""
    conversation_id = _conversation()
    task_id = uuid.uuid4().hex
    voice_session_id = uuid.uuid4().hex
    stamp = now_iso()
    queue: asyncio.Queue[str] = asyncio.Queue()
    releases: list[str] = []

    def failed_notification(*args, **kwargs):
        raise RuntimeError("injected voice telemetry failure")

    monkeypatch.setattr(task_runtime, "_queue", queue)
    monkeypatch.setattr("app.voice.session_manager.emit_voice_event", failed_notification)
    monkeypatch.setattr("app.voice.session_manager.resource_coordinator.release_voice_session", releases.append)
    with connect() as db:
        db.execute(
            "INSERT INTO voice_sessions(voice_session_id,conversation_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
            (voice_session_id, conversation_id, "REVIEWING", stamp, stamp),
        )

    response = TestClient(app).post("/api/tasks", json={
        "conversation_id": conversation_id, "task_id": task_id,
        "voice_session_id": voice_session_id, "content": "synthetic voice input",
    })

    assert response.status_code == 202
    assert _intake_counts(task_id) == dict.fromkeys(_INTAKE_TABLES, 1)
    assert queue.get_nowait() == response.json()["queue_item"]["id"]
    assert releases == [voice_session_id]
    assert rows("SELECT state FROM voice_sessions WHERE voice_session_id=?", (voice_session_id,)) == [
        {"state": "QUEUED_FOR_AGENT"},
    ]


def test_admission_respects_existing_pending_submit_cap(monkeypatch) -> None:
    """DSH-NEXT-INTAKE-05: overload rejection leaves all accepted work intact."""
    monkeypatch.setattr(settings, "max_concurrent_tasks", 1)
    queue: asyncio.Queue[str] = asyncio.Queue()
    monkeypatch.setattr(task_runtime, "_queue", queue)
    conversation_id = _conversation()
    client = TestClient(app)
    for _ in range(10):
        accepted = client.post("/api/tasks", json={"conversation_id": conversation_id, "content": "accepted"})
        assert accepted.status_code == 202
    task_id = uuid.uuid4().hex
    rejected = client.post("/api/tasks", json={
        "conversation_id": conversation_id, "task_id": task_id, "content": "over capacity",
    })
    assert rejected.status_code == 503
    assert _intake_counts(task_id) == dict.fromkeys(_INTAKE_TABLES, 0)
    assert queue.qsize() == 10


def test_admission_rejects_a_stale_conversation_profile_snapshot(monkeypatch) -> None:
    """DSH-NEXT-INTAKE-06: resolver I/O stays outside the lock; drift fails closed."""
    conversation_id = _conversation()
    task_id = uuid.uuid4().hex
    queue: asyncio.Queue[str] = asyncio.Queue()
    require_agent_profile = task_runtime.require_agent_profile

    def resolve_then_switch(profile_id: str):
        profile = require_agent_profile(profile_id)
        with connect() as db:
            db.execute("UPDATE conversations SET agent_profile_id=? WHERE id=?", ("researcher", conversation_id))
        return profile

    monkeypatch.setattr(task_runtime, "_queue", queue)
    monkeypatch.setattr(task_runtime, "require_agent_profile", resolve_then_switch)
    response = TestClient(app).post("/api/tasks", json={
        "conversation_id": conversation_id, "task_id": task_id, "content": "stale profile",
    })
    assert response.status_code == 409
    assert _intake_counts(task_id) == dict.fromkeys(_INTAKE_TABLES, 0)
    assert queue.empty()


def test_committed_intake_survives_lost_wakeup_and_is_dispatched_on_restart(monkeypatch) -> None:
    """DSH-NEXT-INTAKE-03: the durable row, not the in-memory wake, owns work."""
    conversation_id = _conversation()
    task_id = uuid.uuid4().hex
    model_calls: list[str] = []

    class LostWakeQueue(asyncio.Queue[str]):
        def put_nowait(self, item: str) -> None:
            raise RuntimeError("injected process-local wakeup failure")

    async def mock_completion(messages, api_key=None, **kwargs):
        model_calls.append(kwargs["task_id"])
        return {"role": "assistant", "content": "recovered durable intake"}

    monkeypatch.setattr("app.runtime.runner.completion", mock_completion)
    with monkeypatch.context() as scope:
        scope.setattr(task_runtime, "_queue", LostWakeQueue())
        submitted = TestClient(app).post("/api/tasks", json={
            "conversation_id": conversation_id, "task_id": task_id, "content": "synthetic recovery",
        })
        assert submitted.status_code == 202
        assert _intake_counts(task_id) == dict.fromkeys(_INTAKE_TABLES, 1)
        assert model_calls == []

    with TestClient(app) as restarted:
        deadline = time.monotonic() + 5
        snapshot = restarted.get(f"/api/tasks/{task_id}").json()
        queue_state = rows("SELECT status FROM conversation_queue_items WHERE task_id=?", (task_id,))
        while queue_state != [{"status": "consumed"}] and time.monotonic() < deadline:
            time.sleep(0.02)
            snapshot = restarted.get(f"/api/tasks/{task_id}").json()
            queue_state = rows("SELECT status FROM conversation_queue_items WHERE task_id=?", (task_id,))
        assert snapshot["status"] == "completed", snapshot
        assert snapshot["result"]["content"] == "recovered durable intake"
        assert model_calls == [task_id]
        assert queue_state == [{"status": "consumed"}]
        assert rows("SELECT role FROM messages WHERE task_id=? ORDER BY id", (task_id,)) == [
            {"role": "user"}, {"role": "assistant"},
        ]
