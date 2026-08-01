from __future__ import annotations

import asyncio
import uuid
from fastapi.testclient import TestClient

from app.database import connect, now_iso
from app.main import app
from app.tts.manager import create_request, tts_manager


def test_tts_settings_health_and_interrupt_routes(monkeypatch) -> None:
    async def health():
        return {"status": "DEGRADED", "providers": [{"provider": "windows", "status": "ok"}]}

    async def interrupt(*, task_id=None):
        return {"status": "CANCELLED", "task_id": task_id, "cleared_queue": 2}

    monkeypatch.setattr(tts_manager, "health", health)
    monkeypatch.setattr(tts_manager, "interrupt", interrupt)
    with TestClient(app) as client:
        health_response = client.get("/api/tts/health")
        settings_response = client.get("/api/tts/settings")
        interrupted = client.post("/api/tts/interrupt", json={"task_id": "task-1"})
    assert health_response.status_code == 200
    assert health_response.json()["providers"][0]["provider"] == "windows"
    assert settings_response.status_code == 200
    assert interrupted.json() == {"status": "CANCELLED", "task_id": "task-1", "cleared_queue": 2}


def test_tts_settings_reject_invalid_playback_mode() -> None:
    with TestClient(app) as client:
        response = client.put("/api/tts/settings", json={"playback_mode": "INVALID"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TTS_INVALID_SETTINGS"


def test_agent_cancel_endpoint_clears_linked_tts_queue(monkeypatch, tmp_path) -> None:
    async def synthesized(request):
        return {"request_id": request.request_id, "provider": "windows", "status": "READY", "audio_url": "/api/tts/audio/fake", "duration_ms": 100, "sample_rate": 24000, "cached": False, "synthesis_ms": 1, "error": None}

    monkeypatch.setattr(tts_manager, "synthesize", synthesized)
    task_id = uuid.uuid4().hex
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        with connect() as db:
            db.execute("INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)", (task_id, conversation["id"], "running", "tts link", now_iso(), now_iso()))
        asyncio.run(tts_manager.speak(create_request({"request_id": "linked", "idempotency_key": "linked", "task_id": task_id, "text": "停止联动。", "cache": False})))
        assert tts_manager.status()["queue_length"] == 1
        response = client.post(f"/api/tasks/{task_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert tts_manager.status()["queue_length"] == 0
