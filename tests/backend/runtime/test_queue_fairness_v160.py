"""Persistent queue fairness without weakening per-conversation serialization."""

import asyncio
import threading
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_busy_conversation_does_not_starve_another_conversation(tmp_path: Path, monkeypatch) -> None:
    first_started = threading.Event()
    other_started = threading.Event()
    release_first = threading.Event()
    queued_started = threading.Event()
    calls: list[str] = []
    active = 0
    maximum_active = 0
    markers = {name: f"{name}-{uuid.uuid4().hex}" for name in ("first", "queued", "other")}

    async def completion(messages, api_key=None, **kwargs):
        nonlocal active, maximum_active
        prompt = next(str(message.get("content", "")) for message in reversed(messages) if message.get("role") == "user")
        name = next(name for name, marker in markers.items() if marker in prompt)
        calls.append(name)
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            if name == "first":
                first_started.set()
                await asyncio.to_thread(release_first.wait, 15)
            elif name == "queued":
                queued_started.set()
            else:
                other_started.set()
            return {"role": "assistant", "content": "done"}
        finally:
            active -= 1

    monkeypatch.setattr(settings, "max_concurrent_tasks", 2)
    monkeypatch.setattr("app.runtime.runner.completion", completion)
    with TestClient(app) as client:
        conversations = [client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "full"}).json() for _ in range(2)]
        task_ids: dict[str, str] = {}
        try:
            for name, conversation in (("first", conversations[0]), ("queued", conversations[0]), ("other", conversations[1])):
                task_ids[name] = uuid.uuid4().hex
                response = client.post("/api/tasks", json={"conversation_id": conversation["id"], "task_id": task_ids[name], "content": markers[name]})
                assert response.status_code == 202
                if name == "first":
                    assert first_started.wait(5), "first task never entered provider"
            assert other_started.wait(5), "free worker was blocked by another conversation's queued task"
            assert not queued_started.is_set(), "same conversation executed concurrently"
        finally:
            release_first.set()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            statuses = [client.get(f"/api/tasks/{task_id}").json()["status"] for task_id in task_ids.values()]
            if statuses == ["completed"] * 3:
                break
            time.sleep(0.03)
        assert statuses == ["completed"] * 3
        assert sorted(calls) == ["first", "other", "queued"]
        assert maximum_active == 2
