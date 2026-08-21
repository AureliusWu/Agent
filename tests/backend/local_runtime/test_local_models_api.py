from __future__ import annotations

from fastapi.testclient import TestClient

from app.local_runtime.model_manager import model_manager
from app.local_runtime.ollama_service_manager import ollama_service_manager
from app.local_runtime.resource_coordinator import resource_coordinator
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


def test_resources_route_exposes_observable_admission_decisions(monkeypatch) -> None:
    async def running_models() -> list[dict[str, str]]:
        return []

    snapshot = {
        "system_available_bytes": 8 * 1024 * 1024 * 1024,
        "gpu_free_bytes": 4 * 1024 * 1024 * 1024,
    }
    captured: dict[str, object] = {}

    async def service_status() -> dict[str, object]:
        return {"api_healthy": True, "listener_pid": 8765, "status": "MANAGED_RUNNING", "mode": "managed"}

    monkeypatch.setattr(model_manager, "running_models", running_models)
    monkeypatch.setattr(ollama_service_manager(), "status", service_status)

    def resource_snapshot(**kwargs: object) -> dict[str, int]:
        captured.update(kwargs)
        return snapshot

    monkeypatch.setattr(resource_coordinator, "snapshot", resource_snapshot)

    with TestClient(app) as client:
        response = client.get("/api/local-models/resources")

    assert response.status_code == 200
    payload = response.json()
    assert payload["admission"]["voice"]["allowed"] is True
    assert payload["admission"]["stt_cpu"]["allowed"] is True
    assert payload["admission"]["model_preload"]["allowed"] is True
    assert payload["policy"]["text_input_available_under_pressure"] is True
    assert payload["ollama_listener_pid"] == 8765
    assert captured["ollama_pid"] == 8765
