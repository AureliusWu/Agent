from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.local_runtime import resource_coordinator as resource_module
from app.local_runtime.model_manager import ModelManagerError, model_manager
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


@pytest.mark.parametrize("loaded_models", [[], [{"name": "qwen3:4b"}]])
def test_resources_route_exposes_observable_admission_decisions(monkeypatch, loaded_models) -> None:
    async def running_models() -> list[dict[str, str]]:
        return loaded_models

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
    assert payload["ollama_models_observed"] is True
    assert payload["ollama_error_code"] is None
    assert captured["ollama_pid"] == 8765
    assert captured["active_model"] == ("qwen3:4b" if loaded_models else None)


@pytest.mark.parametrize("available_bytes", [617377792, 8 * 1024**3, None])
def test_resources_remain_observable_when_ollama_is_offline(monkeypatch, available_bytes) -> None:
    async def running_models():
        raise ModelManagerError("private endpoint must not leak", "OLLAMA_API_ERROR") from httpx.ConnectError("offline")

    async def service_status():
        return {"api_healthy": False, "listener_pid": None, "status": "INSTALLED_STOPPED", "mode": None}

    monkeypatch.setattr(model_manager, "running_models", running_models)
    monkeypatch.setattr(ollama_service_manager(), "status", service_status)
    monkeypatch.setattr(resource_module, "_memory", lambda: (16 * 1024**3, available_bytes))
    monkeypatch.setattr(resource_module, "_gpu", lambda: (None, None))
    monkeypatch.setattr(resource_module, "_process_rss", lambda _name: None)
    monkeypatch.setattr(resource_module, "_process_rss_by_pid", lambda _pid: None)
    monkeypatch.setattr(resource_module, "_process_rss_by_pids", lambda _pids: None)

    with TestClient(app) as client:
        response = client.get("/api/local-models/resources")

    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot"]["system_available_bytes"] == available_bytes
    assert payload["snapshot"]["active_model"] is None
    assert payload["snapshot"]["ollama_pid"] is None
    assert payload["snapshot"]["ollama_rss_bytes"] is None
    assert payload["ollama_listener_pid"] is None
    assert payload["ollama_models_observed"] is False
    assert payload["ollama_error_code"] == "OLLAMA_API_ERROR"
    assert "private endpoint" not in response.text
    assert payload["policy"]["minimum_available_ram_bytes"] == 2 * 1024**3
    for workload in ("voice", "stt_cpu"):
        admission = payload["admission"][workload]
        pressure = available_bytes is not None and available_bytes < 2 * 1024**3
        assert admission["allowed"] is (not pressure)
        assert admission["reason_code"] == ("RESOURCE_RAM_PRESSURE" if pressure else None)
        assert admission["system_memory_observed"] is (available_bytes is not None)
        assert admission["system_available_bytes"] == available_bytes
        assert admission["minimum_available_ram_bytes"] == 2 * 1024**3
        assert admission["gpu_memory_observed"] is False
        assert admission["gpu_free_bytes"] is None


@pytest.mark.parametrize("error", [RuntimeError("programming error"), ModelManagerError("unexpected state", "MODEL_LOAD_UNCONFIRMED")])
def test_resources_do_not_hide_unexpected_model_errors(monkeypatch, error) -> None:
    async def running_models():
        raise error

    monkeypatch.setattr(model_manager, "running_models", running_models)
    with TestClient(app) as client, pytest.raises(type(error), match=str(error)):
        client.get("/api/local-models/resources")
