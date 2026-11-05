from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import connect, init_db, rows
from app.main import app


_ENDPOINT = "/api/tasks/attention"
_BASE_STAMP = "2026-11-06T08:00:00+00:00"
_LATER_STAMP = "2026-11-06T08:01:00+00:00"
_SECRET_SENTINEL = "synthetic prompt and provider details must not appear"
_TEST_TOKEN = "test-only-siyi-attention-api-token"


@pytest.fixture
def operator(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "database_path", tmp_path / "task-attention.sqlite3")
    monkeypatch.setattr(settings, "api_token", _TEST_TOKEN)
    init_db()
    # Do not start the worker/model runtime for these persisted-state fixtures.
    return TestClient(app, headers={"X-Agent-Api-Token": _TEST_TOKEN})


def _conversation() -> int:
    with connect() as db:
        return int(db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            (_SECRET_SENTINEL, "private-workspace-sentinel", "ask", _BASE_STAMP, _BASE_STAMP),
        ).lastrowid)


def _task(
    conversation_id: int,
    number: int,
    status: str,
    *,
    resumable: int = 1,
    updated_at: str = _BASE_STAMP,
) -> str:
    task_id = f"{number:032x}"
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,termination_reason,last_error,"
            "credential_profile_id,resumable,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, conversation_id, status, _SECRET_SENTINEL, _SECRET_SENTINEL,
                _SECRET_SENTINEL, _SECRET_SENTINEL, resumable, _BASE_STAMP, updated_at,
            ),
        )
    return task_id


def test_attention_is_scoped_bounded_metadata_with_one_authoritative_total(operator: TestClient) -> None:
    """SIYI-1602-ATTENTION-API: state, order, count, scope, and privacy."""
    home, other = _conversation(), _conversation()
    old_recovery = _task(home, 1, "interrupted")
    provider = _task(home, 2, "waiting_provider")
    newer_recovery = _task(home, 3, "timed_out", updated_at=_LATER_STAMP)
    user_input = _task(home, 4, "waiting_user")
    credential = _task(home, 5, "waiting_provider_credential")
    confirmation = _task(home, 6, "waiting_confirmation")
    _task(home, 7, "waiting_confirmation", resumable=0, updated_at=_LATER_STAMP)
    for idx, status in enumerate(("running", "completed", "cancelled", "failed", "blocked", "recovering"), start=10):
        _task(home, idx, status, updated_at=_LATER_STAMP)
    other_confirmation = _task(other, 16, "waiting_confirmation", updated_at=_LATER_STAMP)

    response = operator.get(_ENDPOINT)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["total"] == 7
    assert [item["task_id"] for item in result["items"]] == [
        other_confirmation, confirmation, user_input, credential, provider, newer_recovery, old_recovery,
    ]
    assert [item["category"] for item in result["items"]] == [
        "confirmation", "confirmation", "user_input", "credential", "provider", "recovery", "recovery",
    ]
    assert [item["status"] for item in result["items"]] == [
        "waiting_confirmation", "waiting_confirmation", "waiting_user", "waiting_provider_credential",
        "waiting_provider", "timed_out", "interrupted",
    ]
    assert all(set(item) == {"task_id", "conversation_id", "status", "category", "updated_at"} for item in result["items"])
    assert result["items"][0]["updated_at"] == _LATER_STAMP
    assert _SECRET_SENTINEL not in response.text
    assert "private-workspace-sentinel" not in response.text

    scoped = operator.get(_ENDPOINT, params={"conversation_id": home, "limit": 2}).json()
    assert scoped == {"items": result["items"][1:3], "total": 6}
    assert operator.get(_ENDPOINT, params={"conversation_id": other + 1}).json() == {"items": [], "total": 0}


def test_attention_validates_local_auth_and_input_bounds(operator: TestClient) -> None:
    """SIYI-1602-ATTENTION-SECURITY: no renderer-auth or mutation bypass."""
    anonymous = TestClient(app)
    assert anonymous.get(_ENDPOINT).status_code == 401
    assert anonymous.get(_ENDPOINT, headers={"X-Agent-Api-Token": "wrong"}).status_code == 401
    for params in ({"limit": 0}, {"limit": 101}, {"conversation_id": 0}, {"conversation_id": 1 << 63}):
        assert operator.get(_ENDPOINT, params=params).status_code == 422
    assert operator.get(_ENDPOINT).json() == {"items": [], "total": 0}
    assert operator.post(_ENDPOINT).status_code == 405


def test_attention_refresh_follows_simulated_operator_using_existing_action(operator: TestClient, monkeypatch) -> None:
    """SIYI-1602-ATTENTION-OPERATOR: GET does not resolve; original abandon does."""
    # The original route still updates the real task state in the test DB;
    # no real audio resources, process actions or model calls are needed.
    monkeypatch.setattr("app.api.routes.chat.voice_session_manager.cancel_for_task", AsyncMock())
    monkeypatch.setattr("app.api.routes.chat.tts_manager.interrupt", AsyncMock())
    home, other = _conversation(), _conversation()
    target = _task(home, 21, "waiting_confirmation")
    untouched = _task(other, 22, "waiting_confirmation")
    before = rows("SELECT * FROM agent_tasks ORDER BY id")
    assert operator.get(_ENDPOINT).json()["total"] == 2
    assert operator.get(_ENDPOINT).json()["total"] == 2
    assert rows("SELECT * FROM agent_tasks ORDER BY id") == before
    assert rows("SELECT * FROM task_transitions") == []

    # No approval capability comes from the inbox; act via the existing route.
    cancelled = operator.post(f"/api/tasks/{target}/abandon")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert operator.get(_ENDPOINT, params={"conversation_id": home}).json() == {"items": [], "total": 0}
    remaining = operator.get(_ENDPOINT).json()
    assert remaining["total"] == 1
    assert remaining["items"][0]["task_id"] == untouched
    assert rows("SELECT status,resumable FROM agent_tasks WHERE id=?", (target,)) == [
        {"status": "cancelled", "resumable": 0},
    ]
