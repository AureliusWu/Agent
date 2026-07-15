import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import connect, now_iso
from app.main import app
from app.task_events import emit_task_event


def create_conversation(client: TestClient, workspace: Path) -> dict:
    return client.post(
        "/api/conversations",
        json={"workspace": str(workspace), "permission_mode": "full"},
    ).json()


def test_background_task_returns_before_model_finishes_and_persists_events(tmp_path: Path, monkeypatch) -> None:
    release_model = threading.Event()
    model_finished = threading.Event()

    async def delayed_completion(messages, api_key=None, **kwargs):
        callback = kwargs.get("event_callback")
        if callback:
            callback("model.delta", {"delta": "流式", "phase": kwargs.get("phase")})
        await asyncio.to_thread(release_model.wait, 10)
        model_finished.set()
        return {"role": "assistant", "content": "流式完成"}

    monkeypatch.setattr("app.task_runner.completion", delayed_completion)
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        task_id = uuid.uuid4().hex
        started = time.perf_counter()
        submitted = client.post(
            "/api/tasks",
            json={"conversation_id": conversation["id"], "content": "后台执行", "task_id": task_id},
        )
        elapsed = time.perf_counter() - started
        finished_before_response = model_finished.is_set()
        release_model.set()

        assert submitted.status_code == 202
        assert submitted.json()["status"] in {"pending", "running"}
        assert finished_before_response is False
        assert elapsed < 5.0

        deadline = time.monotonic() + 3
        snapshot = submitted.json()
        while snapshot["status"] in {"pending", "running"} and time.monotonic() < deadline:
            time.sleep(0.03)
            snapshot = client.get(f"/api/tasks/{task_id}").json()

        assert snapshot["status"] == "completed"
        assert snapshot["result"]["content"] == "流式完成"
        events = client.get(f"/api/tasks/{task_id}/events").text
        assert "event: task.created" in events
        assert "event: model.delta" in events
        assert "event: task.completed" in events


def test_event_stream_resumes_after_last_event_id(tmp_path: Path) -> None:
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        task_id = uuid.uuid4().hex
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at, finished_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, conversation["id"], "completed", "resume events", stamp, stamp, stamp),
            )
        first = emit_task_event(task_id, "task.created", {"status": "pending"})
        emit_task_event(task_id, "task.completed", {"status": "completed", "result": {"content": "done"}})

        response = client.get(f"/api/tasks/{task_id}/events", headers={"Last-Event-ID": str(first["id"])})

    assert response.status_code == 200
    assert "event: task.created" not in response.text
    assert "event: task.completed" in response.text
    data_line = next(line for line in response.text.splitlines() if line.startswith("data:"))
    assert json.loads(data_line[5:].strip())["id"] > first["id"]


def test_same_conversation_is_serial_and_cancelled_queue_item_never_runs(tmp_path: Path, monkeypatch) -> None:
    active_calls = 0
    max_active_calls = 0
    prompts_seen: list[str] = []

    async def tracked_completion(messages, api_key=None, **kwargs):
        nonlocal active_calls, max_active_calls
        prompt = str(messages[-1].get("content") or "")
        prompts_seen.append(prompt)
        active_calls += 1
        max_active_calls = max(max_active_calls, active_calls)
        await asyncio.sleep(0.25)
        active_calls -= 1
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr("app.task_runner.completion", tracked_completion)
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        first_id, second_id = uuid.uuid4().hex, uuid.uuid4().hex
        assert client.post("/api/tasks", json={"conversation_id": conversation["id"], "content": "first", "task_id": first_id}).status_code == 202
        assert client.post("/api/tasks", json={"conversation_id": conversation["id"], "content": "second", "task_id": second_id}).status_code == 202
        cancelled = client.post(f"/api/tasks/{second_id}/cancel")
        assert cancelled.status_code == 200

        deadline = time.monotonic() + 4
        first = client.get(f"/api/tasks/{first_id}").json()
        second = client.get(f"/api/tasks/{second_id}").json()
        while first["status"] in {"pending", "running"} and time.monotonic() < deadline:
            time.sleep(0.03)
            first = client.get(f"/api/tasks/{first_id}").json()
            second = client.get(f"/api/tasks/{second_id}").json()

    assert first["status"] == "completed"
    assert second["status"] == "cancelled"
    assert max_active_calls == 1
    assert not any("second" in prompt for prompt in prompts_seen)


def test_persisted_events_redact_credentials_and_approval_capabilities(tmp_path: Path) -> None:
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        task_id = uuid.uuid4().hex
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (task_id, conversation["id"], "waiting_confirmation", "secure events", stamp, stamp),
            )
        emit_task_event(
            task_id,
            "task.paused",
            {"result": {"content": "contains sk-test_DO_NOT_USE_000000000000", "pending_actions": [{"approval_key": "raw-capability"}]}},
        )
        with connect() as db:
            stored = db.execute("SELECT payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()[0]

    assert "sk-test_DO_NOT_USE_000000000000" not in stored
    assert "raw-capability" not in stored
    assert "REDACTED" in stored
