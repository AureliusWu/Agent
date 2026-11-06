import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import connect, init_db, now_iso
from app.main import app
from app.providers.configuration import ProviderConfiguration, save_provider_configuration
from app.runtime import task_runtime
from app.runtime.queue_service import cancel, enqueue, get_item
from app.runtime.recovery import create_checkpoint, load_checkpoint
from app.runtime.runner import cancel_task
from app.runtime.task_events import emit_task_event
from app.runtime.task_leases import acquire_task_lease, release_task_lease
from app.runtime.task_runtime import _emit_task_stream_event
from app.schemas import ChatRequest


@pytest.fixture(autouse=True)
def isolated_task_runtime_database(tmp_path: Path, monkeypatch) -> None:
    # Startup recovers persisted pending tasks. Previous tests must not execute
    # against this test's provider mock. Restarts within one test keep this DB.
    monkeypatch.setattr(settings, "database_path", tmp_path / "task-runtime.db")
    monkeypatch.setattr(settings, "log_path", tmp_path / "task-runtime.log")
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider-settings.json"))
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    init_db()


def create_conversation(client: TestClient, workspace: Path) -> dict:
    return client.post(
        "/api/conversations",
        json={"workspace": str(workspace), "permission_mode": "full"},
    ).json()


def test_task_runtime_database_is_isolated_between_function_scopes(tmp_path: Path, monkeypatch) -> None:
    original_database_path = settings.database_path
    first_database_path = tmp_path / "first-function" / "task-runtime.db"
    pending_task_id = uuid.uuid4().hex
    stamp = now_iso()

    # Exercise the fixture body with two distinct function-owned tmp_path values
    # without depending on pytest's execution order or deleting another DB.
    for index, function_path in enumerate((tmp_path / "first-function", tmp_path / "second-function")):
        with monkeypatch.context() as scope:
            isolated_task_runtime_database.__wrapped__(function_path, scope)
            assert settings.database_path == function_path / "task-runtime.db"
            with connect() as db:
                assert db.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0] == 0
                if index == 0:
                    conversation = db.execute(
                        "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                        ("preceding-function", str(function_path), "ask", stamp, stamp),
                    )
                    db.execute(
                        "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (pending_task_id, conversation.lastrowid, "pending", "foreign pending task", stamp, stamp),
                    )
                else:
                    assert settings.database_path != first_database_path
                    assert db.execute("SELECT COUNT(*) FROM conversation_queue_items").fetchone()[0] == 0

    assert settings.database_path == original_database_path
    with monkeypatch.context() as scope:
        scope.setattr(settings, "database_path", first_database_path)
        with connect() as db:
            assert db.execute("SELECT status FROM agent_tasks WHERE id=?", (pending_task_id,)).fetchone()[0] == "pending"


def test_task_runtime_database_is_preserved_across_client_restarts(tmp_path: Path) -> None:
    database_path = settings.database_path
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at,finished_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, conversation["id"], "completed", "restart marker", stamp, stamp, stamp),
            )

    with TestClient(app) as client:
        assert settings.database_path == database_path
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert response.json()["prompt"] == "restart marker"


def test_voice_tts_dispatch_is_reserved_before_persisting_first_model_delta(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class VoiceManager:
        def reserve_tts_dispatch(self, task_id: str) -> int:
            calls.append(("reserve", task_id))
            return 1

    monkeypatch.setattr(
        "app.runtime.task_runtime.emit_task_event",
        lambda task_id, event, _data: calls.append((event, task_id)),
    )

    _emit_task_stream_event("voice-task", "model.delta", {"delta": "末尾无标点"}, voice_manager=VoiceManager())

    assert calls == [("reserve", "voice-task"), ("model.delta", "voice-task")]


def test_background_task_returns_before_model_finishes_and_persists_events(tmp_path: Path, monkeypatch) -> None:
    release_model = threading.Event()
    model_finished = threading.Event()
    task_id = uuid.uuid4().hex

    async def delayed_completion(messages, api_key=None, **kwargs):
        assert kwargs.get("task_id") == task_id, "mock provider received a different task"
        callback = kwargs.get("event_callback")
        if callback:
            callback("model.delta", {"delta": "流式", "phase": kwargs.get("phase")})
        await asyncio.to_thread(release_model.wait, 10)
        model_finished.set()
        return {"role": "assistant", "content": "流式完成"}

    monkeypatch.setattr("app.runtime.runner.completion", delayed_completion)
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
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

        # GitHub's shared Windows runners can take several seconds to schedule
        # the background worker after the mocked provider is released.  Keep
        # the assertion bounded, but do not turn scheduler latency into a
        # product failure while the task is still making progress.
        assert model_finished.wait(10), "mock provider did not finish after release"
        deadline = time.monotonic() + 10
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


def test_workspace_free_background_mock_stream_persists_full_reply(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "mock-provider.json"))
    monkeypatch.setenv("SIYI_MOCK_SCENARIO", "stream")
    save_provider_configuration(ProviderConfiguration(provider_id="mock", max_retries=0, allow_tools=False))

    async def reject_network(*_args, **_kwargs):
        raise AssertionError("the in-process mock must not send network requests")

    monkeypatch.setattr("httpx.AsyncClient.send", reject_network)
    task_id = uuid.uuid4().hex
    prompt = "你好"
    with TestClient(app) as client:
        created = client.post(
            "/api/conversations",
            json={"title": "离线流式回归", "workspace": "", "permission_mode": "ask"},
        )
        assert created.status_code == 200
        conversation = created.json()
        assert conversation["title_locked"] is True
        submitted = client.post(
            "/api/tasks", json={"conversation_id": conversation["id"], "content": prompt, "task_id": task_id},
        )
        assert submitted.status_code == 202
        deadline = time.monotonic() + 10
        snapshot = submitted.json()
        while snapshot["status"] in {"pending", "running", "verifying"} and time.monotonic() < deadline:
            time.sleep(0.03)
            snapshot = client.get(f"/api/tasks/{task_id}").json()

        assert snapshot["status"] == "completed"
        assert snapshot["result"]["content"] == "mock response"
        with connect() as db:
            messages = db.execute(
                "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id", (conversation["id"],),
            ).fetchall()
            task = db.execute(
                "SELECT status,model_calls,tool_calls,files_modified FROM agent_tasks WHERE id=?", (task_id,),
            ).fetchone()
            events = db.execute(
                "SELECT event_type,payload FROM task_events WHERE task_id=? ORDER BY id", (task_id,),
            ).fetchall()
            assert db.execute("SELECT COUNT(*) FROM conversation_title_jobs").fetchone()[0] == 0
        assert [tuple(message) for message in messages] == [("user", prompt), ("assistant", "mock response")]
        assert tuple(task) == ("completed", 1, 0, 0)
        assert [json.loads(event[1])["delta"] for event in events if event[0] == "model.delta"] == ["mock ", "response"]
        assert "task.completed" in [event[0] for event in events]
        assert "task.failed" not in [event[0] for event in events]


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


def test_v1602_resume_cursor_skips_prior_attempts_terminal_event(tmp_path: Path, monkeypatch) -> None:
    task_id = uuid.uuid4().hex
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    finish_attempt = threading.Event()

    async def completion(*_args, **_kwargs):
        deadline = time.monotonic() + 5
        while not finish_attempt.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return {"role": "assistant", "content": "New attempt finished"}

    monkeypatch.setattr("app.runtime.runner.completion", completion)
    with TestClient(app) as client:
        conversation = create_conversation(client, workspace)
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at,resumable) VALUES(?,?,?,?,?,?,?)",
                (task_id, conversation["id"], "interrupted", "prepare result", stamp, stamp, 1),
            )
        checkpoint = create_checkpoint(task_id, str(workspace), "analysis", "test_interrupt", {
            "executor_messages": [{"role": "user", "content": "prepare result"}],
        })
        old = emit_task_event(task_id, "task.interrupted", {
            "status": "interrupted", "result": {"content": "Old attempt ended"},
        })
        assert client.get(f"/api/tasks/{task_id}").json()["result_event_id"] == old["id"]
        response = client.post(f"/api/tasks/{task_id}/resume", json={"checkpoint_sequence": checkpoint["sequence"]})
        assert response.status_code == 202
        resumed = response.json()
        cursor = resumed["event_cursor"]
        assert cursor > old["id"]
        try:
            assert "result_event_id" not in resumed and "result" not in resumed
            current = client.get(f"/api/tasks/{task_id}").json()
            assert current["event_cursor"] == cursor
            assert "result_event_id" not in current and "result" not in current
            active = client.get("/api/tasks", params={"conversation_id": conversation["id"], "active": "true"}).json()
            assert [(task["id"], task["conversation_id"], task["event_cursor"]) for task in active] == [
                (task_id, conversation["id"], cursor),
            ]
        finally:
            finish_attempt.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = client.get(f"/api/tasks/{task_id}").json()
            if current.get("result_event_id", 0) > cursor:
                break
            time.sleep(0.03)
        assert current["status"] == "completed"
        assert current["event_cursor"] == cursor
        assert current["result_event_id"] > cursor
        assert current["result"]["content"] == "New attempt finished"
        with connect() as db:
            record = db.execute("SELECT task_id,event_type FROM task_events WHERE id=?", (cursor,)).fetchone()
        assert tuple(record) == (task_id, "task.created")
        stream = client.get(f"/api/tasks/{task_id}/events", headers={"Last-Event-ID": str(cursor)})
        assert "event: task.completed" in stream.text
        assert "Old attempt ended" not in stream.text
        assert "event: task.interrupted" not in stream.text


def test_v1700_stream_retention_keeps_latest_attempt_boundary(tmp_path: Path) -> None:
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        task_id = uuid.uuid4().hex
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (task_id, conversation["id"], "completed", "retention fixture", stamp, stamp),
            )
        old = emit_task_event(task_id, "task.created", {"status": "pending"})
        emit_task_event(task_id, "task.interrupted", {"status": "interrupted"})
        latest = emit_task_event(task_id, "task.created", {"status": "resuming", "resume": True})
        with connect() as db:
            db.executemany(
                "INSERT INTO task_events(task_id,event_type,payload,created_at) VALUES(?,?,?,?)",
                [(task_id, "model.delta", "{}", stamp)] * 5000,
            )
        terminal = emit_task_event(task_id, "task.completed", {"result": {"task_id": task_id, "content": "current reply"}})
        snapshot = client.get(f"/api/tasks/{task_id}").json()
        assert snapshot["event_cursor"] == latest["id"]
        assert snapshot["result_event_id"] == terminal["id"]
        assert snapshot["result"]["content"] == "current reply"
        with connect() as db:
            assert db.execute("SELECT count(*) FROM task_events WHERE task_id=?", (task_id,)).fetchone()[0] == 5000
            assert db.execute("SELECT id FROM task_events WHERE id=?", (old["id"],)).fetchone() is None


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

    monkeypatch.setattr("app.runtime.runner.completion", tracked_completion)
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


def test_pending_task_can_be_cancelled_without_running_or_becoming_failed(tmp_path: Path, monkeypatch) -> None:
    prompts_seen: list[str] = []

    async def tracked_completion(messages, api_key=None, **kwargs):
        prompt = str(messages[-1].get("content") or "")
        prompts_seen.append(prompt)
        await asyncio.sleep(0.2)
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr("app.runtime.runner.completion", tracked_completion)
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        first_id, second_id = uuid.uuid4().hex, uuid.uuid4().hex
        assert client.post("/api/tasks", json={"conversation_id": conversation["id"], "content": "first", "task_id": first_id}).status_code == 202
        assert client.post("/api/tasks", json={"conversation_id": conversation["id"], "content": "second", "task_id": second_id}).status_code == 202
        cancelled = client.post(f"/api/tasks/{second_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        deadline = time.monotonic() + 4
        first = client.get(f"/api/tasks/{first_id}").json()
        second = client.get(f"/api/tasks/{second_id}").json()
        while first["status"] in {"pending", "running"} and time.monotonic() < deadline:
            time.sleep(0.03)
            first = client.get(f"/api/tasks/{first_id}").json()
            second = client.get(f"/api/tasks/{second_id}").json()

    assert first["status"] == "completed"
    assert second["status"] == "cancelled"
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
            "task.interrupted",
            {"result": {"content": "contains sk-test_DO_NOT_USE_000000000000", "pending_actions": [{"approval_key": "raw-capability"}]}},
        )
        with connect() as db:
            stored = db.execute("SELECT payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()[0]

    assert "sk-test_DO_NOT_USE_000000000000" not in stored
    assert "raw-capability" not in stored
    assert "REDACTED" in stored


def test_running_task_accepts_steering_at_next_safe_point(tmp_path: Path, monkeypatch) -> None:
    release_first = threading.Event()
    calls: list[list[dict]] = []

    async def steerable_completion(messages, api_key=None, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            await asyncio.to_thread(release_first.wait, 5)
            return {"role": "assistant", "content": "first draft"}
        return {"role": "assistant", "content": "revised after steering"}

    monkeypatch.setattr("app.runtime.runner.completion", steerable_completion)
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        task_id = uuid.uuid4().hex
        submitted = client.post(
            "/api/tasks",
            json={"conversation_id": conversation["id"], "content": "prepare result", "task_id": task_id},
        )
        assert submitted.status_code == 202
        deadline = time.monotonic() + 3
        snapshot = client.get(f"/api/tasks/{task_id}").json()
        while snapshot["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
            snapshot = client.get(f"/api/tasks/{task_id}").json()
        runtime = client.get(f"/api/conversations/{conversation['id']}/runtime").json()
        assert runtime["state"] == "running"
        assert runtime["task_id"] == task_id
        steered = client.post(
            f"/api/tasks/{task_id}/steer",
            json={"content": "focus on the migration risk", "priority": "now", "target_scope": "task"},
        )
        assert steered.status_code == 202
        cancelled_steer = client.post(
            f"/api/tasks/{task_id}/steer",
            json={"content": "discard this guidance", "priority": "now", "target_scope": "task"},
        )
        assert client.delete(f"/api/queue/{cancelled_steer.json()['id']}").status_code == 200
        release_first.set()
        while snapshot["status"] in {"pending", "running"} and time.monotonic() < deadline:
            time.sleep(0.03)
            snapshot = client.get(f"/api/tasks/{task_id}").json()

        assert snapshot["status"] == "completed"
        assert snapshot["result"]["content"] == "revised after steering"
        assert len(calls) == 2
        assert "focus on the migration risk" in str(calls[1])
        assert "discard this guidance" not in str(calls[1])
        assert client.get(f"/api/conversations/{conversation['id']}/queue").json() == []
        assert client.get(f"/api/conversations/{conversation['id']}/runtime").json()["state"] == "idle"


def test_v1602_steering_checkpoints_before_replacing_unstarted_tool_batch(tmp_path: Path, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    task_id = uuid.uuid4().hex
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []
    observations = {}

    async def complete(messages, api_key=None, **kwargs):
        calls.append(json.dumps(messages, ensure_ascii=False))
        if len(calls) == 1:
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "obsolete-write",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": '{"path":"obsolete.txt","content":"wrong"}'},
                }],
            }
        checkpoint = load_checkpoint(task_id)
        if checkpoint and checkpoint["reason"] == "steering_applied":
            observations["checkpoint"] = checkpoint
            observations["item"] = get_item(checkpoint["state"]["steering_item_ids"][0])
        return {"role": "assistant", "content": "will not write"}

    monkeypatch.setattr("app.runtime.runner.completion", complete)
    with TestClient(app) as client:
        conversation = create_conversation(client, workspace)
        assert client.post("/api/tasks", json={
            "conversation_id": conversation["id"], "task_id": task_id,
            "content": "prepare result",
        }).status_code == 202
        try:
            assert entered.wait(3)
            steered = client.post(f"/api/tasks/{task_id}/steer", json={"content": "stop writing; just explain"})
            assert steered.status_code == 202
        finally:
            release.set()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = client.get(f"/api/tasks/{task_id}").json()
            if result["status"] not in {"pending", "running"}:
                break
            time.sleep(0.02)

    assert result["tool_calls"] == 0
    assert result["status"] == "completed", result.get("current_step")
    checkpoint = observations["checkpoint"]
    assert len(calls) == 2
    assert calls[1].count("stop writing; just explain") == 1
    assert "obsolete-write" not in calls[1]
    assert checkpoint["reason"] == "steering_applied"
    assert checkpoint["state"]["pending_tool_calls"] == []
    assert "stop writing; just explain" in str(checkpoint["state"]["executor_messages"])
    assert observations["item"].id == steered.json()["id"]
    assert observations["item"].status == "consumed"
    assert not (workspace / "obsolete.txt").exists()
    with connect() as db:
        assert db.execute("SELECT COUNT(*) FROM task_operations WHERE task_id=?", (task_id,)).fetchone()[0] == 0
        events = db.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND event_type='queue.consumed'", (task_id,)
        ).fetchall()
    assert [json.loads(event["payload"])["checkpoint_sequence"] for event in events] == [checkpoint["sequence"]]


def test_v1602_steering_ack_and_checkpoint_rollback_together(tmp_path: Path) -> None:
    from app.runtime.recovery import SteeringCheckpointConflict

    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        conversation_id = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("steering", str(tmp_path), "ask", stamp, stamp),
        ).lastrowid
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "test", stamp, stamp),
        )
    first = enqueue(conversation_id=conversation_id, task_id=task_id, kind="steer", content="keep")
    second = enqueue(conversation_id=conversation_id, task_id=task_id, kind="steer", content="drop")
    cancel(second.id)
    baseline = create_checkpoint(task_id, "", "analysis", "baseline", {})
    with pytest.raises(SteeringCheckpointConflict):
        create_checkpoint(
            task_id, "", "analysis", "steering_applied", {"executor_messages": [{"role": "user", "content": "keep"}]},
            steering_item_ids=(first.id, second.id),
        )
    assert get_item(first.id).status == "pending"
    assert get_item(second.id).status == "cancelled"
    assert load_checkpoint(task_id)["sequence"] == baseline["sequence"]
    with connect() as db:
        assert db.execute(
            "SELECT checkpoint_sequence FROM agent_tasks WHERE id=?", (task_id,)
        ).fetchone()[0] == baseline["sequence"]


def test_v1602_background_resume_rejects_superseded_checkpoint_without_failing_task(tmp_path: Path, monkeypatch) -> None:
    task_id = uuid.uuid4().hex
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with TestClient(app) as client:
        conversation = create_conversation(client, workspace)
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at,resumable) VALUES(?,?,?,?,?,?,?)",
                (task_id, conversation["id"], "interrupted", "prepare result", stamp, stamp, 1),
            )
        old = create_checkpoint(task_id, str(workspace), "analysis", "pre_steer", {"pending_final_response": "obsolete"})
        guidance = enqueue(
            conversation_id=conversation["id"], task_id=task_id, kind="steer", content="new guidance"
        )
        create_checkpoint(task_id, str(workspace), "analysis", "steering_applied", {
            "executor_messages": [{"role": "user", "content": guidance.content}],
        }, steering_item_ids=(guidance.id,))
        endpoint = f"/api/tasks/{task_id}/resume"
        body = {"checkpoint_sequence": old["sequence"]}
        rejected = client.post(endpoint, json=body)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "checkpoint_superseded_by_steering"

        # Simulate a checkpoint advancing after API admission, before the
        # worker takes its lease. The worker must refuse without a terminal write.
        monkeypatch.setattr("app.api.routes.chat.predates_applied_steering", lambda *_: False)
        accepted = client.post(endpoint, json=body)
        assert accepted.status_code == 202
        item_id = accepted.json()["queue_item"]["id"]
        deadline = time.monotonic() + 5
        while get_item(item_id).status != "consumed" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert get_item(item_id).status == "consumed"
        snapshot = client.get(f"/api/tasks/{task_id}").json()
        assert snapshot["status"] == "interrupted"
        assert snapshot["resumable"]
        assert snapshot["model_calls"] == snapshot["tool_calls"] == 0
        with connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND event_type='task.failed'", (task_id,)
            ).fetchone()[0] == 0
        stream = client.get(f"/api/tasks/{task_id}/events", params={"after_id": accepted.json()["event_cursor"]})
        assert "event: task.interrupted" in stream.text
        assert "checkpoint_superseded_by_steering" in stream.text


def _seed_resume_task(workspace: Path, task_status: str) -> tuple[int, str]:
    task_id, stamp = uuid.uuid4().hex, now_iso()
    with connect() as db:
        conversation_id = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("resume regression", str(workspace), "ask", stamp, stamp),
        ).lastrowid
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at,resumable) VALUES(?,?,?,?,?,?,?)",
            (task_id, conversation_id, task_status, "resume fixture", stamp, stamp, int(task_status == "interrupted")),
        )
    return int(conversation_id), task_id


@pytest.mark.parametrize("status", ["cancelled", "completed", "pending"])
def test_v1603_resume_rejects_nonresumable_task_without_queue_or_event(status: str, tmp_path: Path, monkeypatch) -> None:
    _, task_id = _seed_resume_task(tmp_path, status)
    queue: asyncio.Queue[str] = asyncio.Queue()
    monkeypatch.setattr(task_runtime, "_queue", queue)

    # No lifespan: admission must refuse before touching the test-owned queue.
    response = TestClient(app).post(f"/api/tasks/{task_id}/resume", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "task_not_resumable"
    assert response.json()["detail"]["status"] == status
    assert queue.empty()
    with connect() as db:
        assert db.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0] == status
        assert db.execute("SELECT COUNT(*) FROM conversation_queue_items WHERE task_id=?", (task_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,)).fetchone()[0] == 0


def test_v1603_resume_does_not_duplicate_persisted_admission_when_wakeup_is_lost(tmp_path: Path, monkeypatch) -> None:
    _, task_id = _seed_resume_task(tmp_path, "interrupted")

    class FailedWakeup(asyncio.Queue[str]):
        def put_nowait(self, item: str) -> None:
            raise RuntimeError("synthetic lost wakeup")

    monkeypatch.setattr(task_runtime, "_queue", FailedWakeup())
    monkeypatch.setattr(task_runtime, "_scheduled_task_ids", set())
    monkeypatch.setattr(task_runtime, "_ephemeral_credentials", {})
    client = TestClient(app)
    endpoint = f"/api/tasks/{task_id}/resume"
    initial = client.post(endpoint, json={})
    assert initial.status_code == 202
    task_runtime._scheduled_task_ids.clear()  # A new process has no local wakeup state.
    retry = client.post(endpoint, json={})
    assert retry.status_code == 409
    assert get_item(initial.json()["queue_item"]["id"]).status == "pending"
    with connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversation_queue_items WHERE task_id=?", (task_id,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,)).fetchone()[0] == 1


def test_v1603_resume_refuses_admission_while_task_has_live_lease(tmp_path: Path, monkeypatch) -> None:
    _, task_id = _seed_resume_task(tmp_path, "interrupted")
    queue: asyncio.Queue[str] = asyncio.Queue()
    monkeypatch.setattr(task_runtime, "_queue", queue)
    lease = acquire_task_lease(task_id)
    try:
        response = TestClient(app).post(f"/api/tasks/{task_id}/resume", json={})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "owned_by_other_runtime"
        assert queue.empty()
        with connect() as db:
            assert db.execute("SELECT COUNT(*) FROM conversation_queue_items WHERE task_id=?", (task_id,)).fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (task_id,)).fetchone()[0] == 0
    finally:
        assert release_task_lease(lease)


def test_v1603_recovered_resume_row_does_not_start_task_abandoned_before_dispatch(tmp_path: Path, monkeypatch) -> None:
    conversation_id, task_id = _seed_resume_task(tmp_path, "interrupted")
    request = ChatRequest(conversation_id=conversation_id, content="resume fixture", task_id=task_id, resume=True)
    item = enqueue(
        conversation_id=conversation_id, task_id=task_id, kind="resume", content=request.content,
        payload=request.model_dump(mode="json"),
    )
    created = emit_task_event(task_id, "task.created", {"status": "resuming", "resume": True})
    assert cancel_task(task_id)["status"] == "cancelled"
    attempts = []

    async def mock_run(*args, **kwargs):
        attempts.append(args[0].task_id)
        raise AssertionError("a cancelled task must never reach the runner")

    monkeypatch.setattr(task_runtime, "run_chat", mock_run)
    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        while get_item(item.id).status != "consumed" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert get_item(item.id).status == "consumed"
        assert client.get(f"/api/tasks/{task_id}").json()["status"] == "cancelled"
        stream = client.get(f"/api/tasks/{task_id}/events", params={"after_id": created["id"]})
        assert "event: task.cancelled" in stream.text
        assert "event: task.started" not in stream.text
        assert "event: task.failed" not in stream.text
    assert attempts == []


def test_v1603_resume_cancelled_after_dispatch_keeps_user_stop_and_closes_stream(tmp_path: Path, monkeypatch) -> None:
    _, task_id = _seed_resume_task(tmp_path, "interrupted")
    actual_run = task_runtime.run_chat

    async def cancel_after_worker_start(*args, **kwargs):
        assert cancel_task(task_id)["status"] == "cancelled"
        return await actual_run(*args, **kwargs)

    monkeypatch.setattr(task_runtime, "run_chat", cancel_after_worker_start)
    with TestClient(app) as client:
        accepted = client.post(f"/api/tasks/{task_id}/resume", json={})
        assert accepted.status_code == 202
        stream = client.get(f"/api/tasks/{task_id}/events", params={"after_id": accepted.json()["event_cursor"]})
        assert "event: task.started" in stream.text
        assert "event: task.cancelled" in stream.text
        assert "event: task.failed" not in stream.text
        assert client.get(f"/api/tasks/{task_id}").json()["status"] == "cancelled"


def test_v1603_resume_drift_refusal_keeps_checkpoint_and_allows_explicit_retry(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tracked = workspace / "notes.txt"
    tracked.write_text("original", encoding="utf-8")
    _, task_id = _seed_resume_task(workspace, "interrupted")
    checkpoint = create_checkpoint(task_id, str(workspace), "analysis", "resume test", {
        "expected_paths": ["notes.txt"],
        "executor_messages": [{"role": "user", "content": "resume fixture"}],
    })
    tracked.write_text("changed", encoding="utf-8")
    calls = []

    async def completion(*_args, **kwargs):
        calls.append(kwargs["task_id"])
        return {"role": "assistant", "content": "Resumed after explicit confirmation"}

    monkeypatch.setattr("app.runtime.runner.completion", completion)
    with TestClient(app) as client:
        endpoint = f"/api/tasks/{task_id}/resume"
        accepted = client.post(endpoint, json={"checkpoint_sequence": checkpoint["sequence"]})
        assert accepted.status_code == 202
        receipt = accepted.json()
        stream = client.get(f"/api/tasks/{task_id}/events", params={"after_id": receipt["event_cursor"]})
        records = [json.loads(line[5:]) for line in stream.text.splitlines() if line.startswith("data:")]
        assert records[-1]["event"] == "task.interrupted"
        assert records[-1]["payload"]["code"] == "workspace_drift"
        assert records[-1]["payload"]["resume_rejected"] is True
        assert records[-1]["payload"]["status"] == "interrupted"
        assert "result" not in records[-1]["payload"]
        assert calls == []
        assert load_checkpoint(task_id)["sequence"] == checkpoint["sequence"]
        snapshot = client.get(f"/api/tasks/{task_id}").json()
        assert snapshot["status"] == "interrupted"
        assert snapshot["resumable"]
        deadline = time.monotonic() + 5
        while get_item(receipt["queue_item"]["id"]).status != "consumed" and time.monotonic() < deadline:
            time.sleep(0.02)
        resumed = client.post(endpoint, json={"checkpoint_sequence": checkpoint["sequence"], "allow_workspace_drift": True})
        assert resumed.status_code == 202
        stream = client.get(f"/api/tasks/{task_id}/events", params={"after_id": resumed.json()["event_cursor"]})
        assert "event: task.completed" in stream.text
        assert "Resumed after explicit confirmation" in stream.text
    assert calls and set(calls) == {task_id}


def test_queue_promote_and_cancel_change_persisted_schedule(tmp_path: Path, monkeypatch) -> None:
    release_first = threading.Event()
    prompts_seen: list[str] = []
    ids = [uuid.uuid4().hex for _ in range(3)]

    async def ordered_completion(messages, api_key=None, **kwargs):
        assert kwargs.get("task_id") in ids, "mock provider received a different task"
        prompt = str(messages)
        prompts_seen.append(prompt)
        if kwargs["task_id"] == ids[0]:
            await asyncio.to_thread(release_first.wait, 5)
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr("app.runtime.runner.completion", ordered_completion)
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        for task_id, content in zip(ids, ("first", "second", "third"), strict=True):
            assert client.post(
                "/api/tasks",
                json={"conversation_id": conversation["id"], "content": content, "task_id": task_id},
            ).status_code == 202
        deadline = time.monotonic() + 4
        queue = client.get(f"/api/conversations/{conversation['id']}/queue").json()
        while len(queue) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
            queue = client.get(f"/api/conversations/{conversation['id']}/queue").json()
        second = next(item for item in queue if item["task_id"] == ids[1])
        third = next(item for item in queue if item["task_id"] == ids[2])
        assert client.post(f"/api/queue/{third['id']}/promote", json={"priority": "next"}).status_code == 200
        assert client.delete(f"/api/queue/{second['id']}").status_code == 200
        reordered = client.get(f"/api/conversations/{conversation['id']}/queue").json()
        assert [item["task_id"] for item in reordered] == [ids[2]]
        release_first.set()
        completion_deadline = time.monotonic() + 4
        third_snapshot = client.get(f"/api/tasks/{ids[2]}").json()
        while third_snapshot["status"] in {"pending", "running"} and time.monotonic() < completion_deadline:
            time.sleep(0.03)
            third_snapshot = client.get(f"/api/tasks/{ids[2]}").json()

        assert third_snapshot["status"] == "completed"
        assert client.get(f"/api/tasks/{ids[1]}").json()["status"] == "cancelled"
        assert any("third" in prompt for prompt in prompts_seen)
        assert not any("second" in prompt for prompt in prompts_seen)


def test_pending_queue_survives_runtime_restart(tmp_path: Path, monkeypatch) -> None:
    async def blocked_completion(messages, api_key=None, **kwargs):
        await asyncio.sleep(30)
        return {"role": "assistant", "content": "unexpected"}

    monkeypatch.setattr("app.runtime.runner.completion", blocked_completion)
    with TestClient(app) as client:
        conversation = create_conversation(client, tmp_path)
        first_id, second_id = uuid.uuid4().hex, uuid.uuid4().hex
        assert client.post(
            "/api/tasks", json={"conversation_id": conversation["id"], "content": "blocking", "task_id": first_id}
        ).status_code == 202
        deadline = time.monotonic() + 3
        first = client.get(f"/api/tasks/{first_id}").json()
        while first["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
            first = client.get(f"/api/tasks/{first_id}").json()
        assert client.post(
            "/api/tasks", json={"conversation_id": conversation["id"], "content": "persist me", "task_id": second_id}
        ).status_code == 202

    async def recovered_completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "recovered after restart"}

    monkeypatch.setattr("app.runtime.runner.completion", recovered_completion)
    with TestClient(app) as client:
        deadline = time.monotonic() + 4
        second = client.get(f"/api/tasks/{second_id}").json()
        while second["status"] in {"pending", "running"} and time.monotonic() < deadline:
            time.sleep(0.03)
            second = client.get(f"/api/tasks/{second_id}").json()

    assert second["status"] == "completed"
    assert second["result"]["content"] == "recovered after restart"
