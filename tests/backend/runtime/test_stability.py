import asyncio
from contextlib import closing
import logging
import os
import sqlite3
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
from app.runtime import runner as task_runner


SIYI_ROOT = Path(__file__).resolve().parents[3] / "siyi"


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


def test_log_rotation_enforces_size_and_backup_retention(
    tmp_path: Path, monkeypatch
) -> None:
    log_path = tmp_path / "logs" / "agent.log"
    monkeypatch.setattr("app.logging_config.settings.log_path", log_path)
    monkeypatch.setattr("app.logging_config.settings.log_max_bytes", 1024)
    monkeypatch.setattr("app.logging_config.settings.log_backup_count", 2)
    configure_logging()
    logger = logging.getLogger("agent")
    for index in range(100):
        logger.info("rotation-%03d-%s", index, "x" * 120)
    for handler in logger.handlers:
        handler.flush()

    files = sorted(log_path.parent.glob("agent.log*"))
    assert {path.name for path in files} == {"agent.log", "agent.log.1", "agent.log.2"}
    assert all(path.stat().st_size < 1400 for path in files)


def test_sidecar_process_uses_dynamic_port_and_stops_cleanly(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = os.environ.copy()
    env.update({
        "AGENT_PORT": str(port),
        "AGENT_DATA_ROOT": str(tmp_path / "runtime"),
        "AGENT_DATABASE_PATH": str(tmp_path / "sidecar.db"),
        "AGENT_LOG_PATH": str(tmp_path / "sidecar.log"),
    })
    process = subprocess.Popen([sys.executable, "run_server.py"], cwd=SIYI_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
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


def test_desktop_sidecar_shutdown_is_authenticated_and_persists_pending_task(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    database = tmp_path / "desktop.db"
    token = "desktop-lifecycle-test-token"
    env = os.environ.copy()
    env.update({
        "AGENT_PORT": str(port),
        "AGENT_DATA_ROOT": str(tmp_path / "runtime"),
        "AGENT_DATABASE_PATH": str(database),
        "AGENT_LOG_PATH": str(tmp_path / "desktop.log"),
        "AGENT_DEPLOYMENT_MODE": "desktop_local",
        "AGENT_BIND_HOST": "127.0.0.1",
        "AGENT_API_TOKEN": token,
    })
    process = subprocess.Popen([sys.executable, "run_server.py"], cwd=SIYI_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    restarted = None
    try:
        deadline = time.monotonic() + 30
        response = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert response is not None and response.status_code == 200
        assert httpx.get(f"http://127.0.0.1:{port}/api/desktop/status", timeout=1).status_code == 401
        headers = {"X-Agent-Api-Token": token}
        assert httpx.get(f"http://127.0.0.1:{port}/api/desktop/status", headers=headers, timeout=1).json()["status"] == "ok"

        stamp = now_iso()
        with closing(sqlite3.connect(database)) as db, db:
            db.execute(
                "INSERT INTO conversations(title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?)",
                ("shutdown", str(tmp_path), "ask", stamp, stamp),
            )
            conversation_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("pending-shutdown", conversation_id, "pending", "wait", stamp, stamp),
            )
        shutdown = httpx.post(f"http://127.0.0.1:{port}/api/desktop/shutdown", headers=headers, timeout=2)
        assert shutdown.status_code == 202
        process.wait(timeout=10)
        with closing(sqlite3.connect(database)) as db, db:
            status = db.execute("SELECT status FROM agent_tasks WHERE id='pending-shutdown'").fetchone()[0]
        assert status == "pending"

        new_token = "desktop-lifecycle-restarted-token"
        restarted_env = {**env, "AGENT_API_TOKEN": new_token}
        restarted = subprocess.Popen(
            [sys.executable, "run_server.py"],
            cwd=SIYI_ROOT,
            env=restarted_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert httpx.get(
            f"http://127.0.0.1:{port}/api/desktop/status",
            headers={"X-Agent-Api-Token": token},
            timeout=1,
        ).status_code == 401
        new_headers = {"X-Agent-Api-Token": new_token}
        assert httpx.get(
            f"http://127.0.0.1:{port}/api/desktop/status",
            headers=new_headers,
            timeout=1,
        ).status_code == 200
        assert httpx.post(
            f"http://127.0.0.1:{port}/api/desktop/shutdown",
            headers=new_headers,
            timeout=2,
        ).status_code == 202
        restarted.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if restarted is not None and restarted.poll() is None:
            restarted.kill()
            restarted.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows parent-process monitor")
def test_desktop_sidecar_stops_after_monitored_parent_exits(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    env = os.environ.copy()
    env.update({
        "AGENT_PORT": str(port),
        "AGENT_PARENT_PID": str(parent.pid),
        "AGENT_DATA_ROOT": str(tmp_path / "runtime"),
        "AGENT_DATABASE_PATH": str(tmp_path / "parent-watch.db"),
        "AGENT_LOG_PATH": str(tmp_path / "parent-watch.log"),
    })
    sidecar = subprocess.Popen([sys.executable, "run_server.py"], cwd=SIYI_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            try:
                ready = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5).status_code == 200
                if ready:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert ready
        parent.terminate()
        parent.wait(timeout=5)
        sidecar.wait(timeout=10)
        assert sidecar.poll() is not None
    finally:
        if parent.poll() is None:
            parent.kill()
        if sidecar.poll() is None:
            sidecar.kill()
            sidecar.wait(timeout=5)
