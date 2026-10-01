from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app
from app.stt.schemas import STTError
from app.voice.session_manager import VoiceSessionError


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


@pytest.mark.parametrize("operation", ["create", "load", "complete"])
@pytest.mark.parametrize("kind", ["ram", "vram"])
def test_stt_api_resource_pressure_returns_only_safe_numeric_details(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    kind: str,
) -> None:
    resource = {
        "kind": kind,
        "available_bytes": 617_377_792,
        "minimum_available_bytes": 2_147_483_648,
        "process_name": "private-process",
        "audio_path": "private-audio-path",
    }
    code = f"RESOURCE_{kind.upper()}_PRESSURE"

    async def refuse(*_args: object, **_kwargs: object) -> None:
        error_type = STTError if operation == "load" else VoiceSessionError
        raise error_type("Resource pressure", code, resource_details=resource)

    manager = "stt_manager" if operation == "load" else "voice_session_manager"
    method = "load_model" if operation == "load" else operation
    monkeypatch.setattr(f"app.api.routes.stt.{manager}.{method}", refuse)
    with TestClient(app) as client:
        if operation == "load":
            response = client.post("/api/stt/models/load", json={"model_id": "small"})
        elif operation == "create":
            response = client.post("/api/stt/sessions", json={"conversation_id": 1})
        else:
            response = client.post(
                f"/api/stt/sessions/{'a' * 32}/complete",
                files={"file": ("recording.wav", b"test-only", "audio/wav")},
            )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": code,
        "message": "Resource pressure",
        "resource": {
            "kind": kind,
            "available_bytes": 617_377_792,
            "minimum_available_bytes": 2_147_483_648,
        },
    }


@pytest.mark.parametrize(
    "resource",
    [
        None,
        {"kind": "vram", "available_bytes": 1, "minimum_available_bytes": 2},
        {"kind": "ram", "available_bytes": -1, "minimum_available_bytes": 2},
        {"kind": "ram", "available_bytes": True, "minimum_available_bytes": 2},
        {"kind": "ram", "available_bytes": "1", "minimum_available_bytes": 2},
        {"kind": "ram", "available_bytes": 1.5, "minimum_available_bytes": 2},
        {"kind": "ram", "available_bytes": 1, "minimum_available_bytes": -2},
        {"kind": "ram", "available_bytes": 1, "minimum_available_bytes": False},
        {"kind": "ram", "available_bytes": 1},
    ],
)
def test_stt_api_invalid_or_legacy_resource_details_are_omitted(
    monkeypatch: pytest.MonkeyPatch,
    resource: dict[str, object] | None,
) -> None:
    async def refuse(*_args: object, **_kwargs: object) -> None:
        error = STTError("Resource pressure", "RESOURCE_RAM_PRESSURE")
        error.resource_details = resource
        raise error

    monkeypatch.setattr("app.api.routes.stt.stt_manager.load_model", refuse)
    with TestClient(app) as client:
        response = client.post("/api/stt/models/load", json={"model_id": "small"})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "RESOURCE_RAM_PRESSURE",
        "message": "Resource pressure",
    }


def test_stt_api_unrelated_error_cannot_emit_untyped_resource_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(*_args: object, **_kwargs: object) -> None:
        raise STTError(
            "Model unavailable",
            "STT_MODEL_MISSING",
            resource_details={"available_bytes": 1, "minimum_available_bytes": 2},
        )

    monkeypatch.setattr("app.api.routes.stt.stt_manager.load_model", refuse)
    with TestClient(app) as client:
        response = client.post("/api/stt/models/load", json={"model_id": "small"})

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "STT_MODEL_MISSING",
        "message": "Model unavailable",
    }


@pytest.mark.parametrize("operation", ["load", "create"])
def test_stt_api_legacy_exception_without_resource_attribute_still_returns_409(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    async def refuse(*_args: object, **_kwargs: object) -> None:
        error_type = STTError if operation == "load" else VoiceSessionError
        error = error_type("Legacy resource pressure", "RESOURCE_RAM_PRESSURE")
        del error.resource_details
        raise error

    target = "stt_manager.load_model" if operation == "load" else "voice_session_manager.create"
    monkeypatch.setattr(f"app.api.routes.stt.{target}", refuse)
    with TestClient(app) as client:
        response = (
            client.post("/api/stt/models/load", json={"model_id": "small"})
            if operation == "load"
            else client.post("/api/stt/sessions", json={"conversation_id": 1})
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "RESOURCE_RAM_PRESSURE",
        "message": "Legacy resource pressure",
    }
