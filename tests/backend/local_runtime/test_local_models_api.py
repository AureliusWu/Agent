from __future__ import annotations

from fastapi.testclient import TestClient

from app.local_runtime.model_manager import model_manager
from app.local_runtime.ollama_service_manager import ollama_service_manager
from app.main import app


def test_local_model_service_and_model_routes(monkeypatch) -> None:
    async def status():
        return {"status": "EXTERNAL_RUNNING", "mode": "external", "listener_pid": 1234}

    async def models():
        return [{"name": "qwen3:4b", "loaded": False, "quantization": "Q4_K_M"}]

    monkeypatch.setattr(ollama_service_manager(), "status", status)
    monkeypatch.setattr(model_manager, "list_models", models)
    with TestClient(app) as client:
        service = client.get("/api/local-models/service")
        installed = client.get("/api/local-models/models")
    assert service.status_code == 200 and service.json()["mode"] == "external"
    assert installed.status_code == 200 and installed.json()[0]["name"] == "qwen3:4b"


def test_model_download_requires_confirmation() -> None:
    with TestClient(app) as client:
        response = client.post("/api/local-models/download", json={"model": "qwen3:4b", "confirmed": False})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DOWNLOAD_CONFIRMATION_REQUIRED"
