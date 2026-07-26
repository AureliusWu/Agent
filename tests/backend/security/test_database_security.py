from __future__ import annotations

import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from app.config import settings
from app.database import connect, now_iso
from app.main import app


def test_conversation_input_is_parameterized_against_sql_injection() -> None:
    malicious = "x'); DROP TABLE conversations; --"
    with TestClient(app) as client:
        response = client.post(
            "/api/conversations",
            json={"title": malicious, "workspace": "", "permission_mode": "ask"},
        )
    assert response.status_code == 200
    with connect() as db:
        stored = db.execute(
            "SELECT title FROM conversations WHERE id=?", (response.json()["id"],)
        ).fetchone()
        table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
        ).fetchone()
    assert stored[0] == malicious
    assert table[0] == "conversations"


def test_transient_database_lock_retries_within_busy_timeout() -> None:
    completed = threading.Event()
    errors: list[Exception] = []

    def writer() -> None:
        try:
            with connect() as db:
                db.execute(
                    "INSERT INTO audit_logs(conversation_id,action,target,status,details,created_at) "
                    "VALUES(NULL,?,?,?,?,?)",
                    ("lock-retry", "database", "ok", "{}", now_iso()),
                )
        except Exception as exc:
            errors.append(exc)
        finally:
            completed.set()

    lock = sqlite3.connect(settings.database_path)
    try:
        lock.execute("BEGIN EXCLUSIVE")
        thread = threading.Thread(target=writer)
        started = time.monotonic()
        thread.start()
        time.sleep(0.2)
        assert not completed.is_set()
        lock.commit()
        thread.join(timeout=3)
        assert completed.is_set()
        assert not errors
        assert time.monotonic() - started < 3
    finally:
        lock.close()
