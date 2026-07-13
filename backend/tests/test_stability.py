import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app.database import connect, database_status, init_db, now_iso
from app.logging_config import configure_logging
from app.schemas import ChatRequest
from app import task_runner


def conversation(tmp_path: Path) -> int:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "stability", str(tmp_path), "full", now_iso(), now_iso()),
        )
    return conversation_id


def test_task_queue_rejects_after_configured_wait(tmp_path: Path, monkeypatch) -> None:
    started = asyncio.Event()

    async def slow_completion(messages, api_key=None, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(task_runner, "completion", slow_completion)
    monkeypatch.setattr(task_runner, "_task_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(task_runner.settings, "task_queue_timeout_seconds", 0.05)
    first_id, second_id = conversation(tmp_path), conversation(tmp_path)

    async def scenario() -> int:
        first = asyncio.create_task(task_runner.run_chat(ChatRequest(conversation_id=first_id, content="hold", task_id=uuid.uuid4().hex)))
        await started.wait()
        with pytest.raises(HTTPException) as caught:
            await task_runner.run_chat(ChatRequest(conversation_id=second_id, content="queued", task_id=uuid.uuid4().hex))
        first.cancel()
        await first
        return caught.value.status_code

    assert asyncio.run(scenario()) == 503


def test_logging_and_database_health(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "logs" / "agent.log"
    monkeypatch.setattr("app.logging_config.settings.log_path", log_path)
    configure_logging()
    logging.getLogger("agent").info("health-check")
    assert "health-check" in log_path.read_text(encoding="utf-8")
    init_db()
    assert database_status()["status"] == "ok"


def test_sidecar_process_uses_dynamic_port_and_stops_cleanly(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = os.environ.copy()
    env.update({
        "AGENT_PORT": str(port),
        "AGENT_DATABASE_PATH": str(tmp_path / "sidecar.db"),
        "AGENT_LOG_PATH": str(tmp_path / "sidecar.log"),
    })
    process = subprocess.Popen([sys.executable, "run_server.py"], cwd=Path(__file__).parents[1], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 12
        response = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert response is not None and response.json()["status"] == "ok"
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert process.poll() is not None
