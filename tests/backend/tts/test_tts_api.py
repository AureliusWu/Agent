from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.tts.manager import tts_manager


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
