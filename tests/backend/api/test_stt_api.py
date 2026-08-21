from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app


def test_stt_api_exposes_small_as_new_install_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.database.settings.database_path", tmp_path / "fresh-default.db")
    init_db()
    with TestClient(app) as client:
        settings = client.get("/api/stt/settings")
        models = client.get("/api/stt/models")

    assert settings.status_code == 200
    assert settings.json()["model_id"] == "small"
    assert models.status_code == 200
    recommendations = {
        model["id"]: model["default_for_new_install"] for model in models.json()
    }
    assert recommendations == {"base": False, "small": True}


def test_stt_api_requires_explicit_model_download_confirmation() -> None:
    with TestClient(app) as client:
        response = client.post("/api/stt/models/download", json={"model_id": "base"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STT_DOWNLOAD_CONFIRMATION_REQUIRED"


def test_stt_api_rejects_unknown_voice_session_and_missing_controlled_upload() -> None:
    session_id = "f" * 32
    with TestClient(app) as client:
        missing_session = client.post(f"/api/stt/sessions/{session_id}/recording-started", json={"device_id": "mic"})
        missing_file = client.post("/api/stt/transcribe", data={"voice_session_id": session_id, "audio_path": "untrusted.wav"})

    assert missing_session.status_code == 404
    assert missing_session.json()["detail"]["code"] == "VOICE_SESSION_NOT_FOUND"
    assert missing_file.status_code == 422
