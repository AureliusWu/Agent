from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.local_runtime.model_manager import ModelManager, ModelManagerError
from app.local_runtime.ollama_discovery import discover_ollama, validate_local_ollama_url
from app.local_runtime.ollama_service_manager import OllamaServiceError, OllamaServiceManager


def test_discovery_prefers_explicit_executable(tmp_path: Path) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    result = discover_ollama(str(executable))
    assert result.installed is True
    assert result.source == "configured"
    assert result.executable == str(executable.resolve())


@pytest.mark.parametrize("url", ["https://127.0.0.1:11434", "http://192.168.1.2:11434", "http://example.com:11434"])
def test_lifecycle_rejects_non_loopback_or_https(url: str) -> None:
    with pytest.raises(ValueError):
        validate_local_ollama_url(url)


def test_external_service_is_never_stopped(monkeypatch, tmp_path: Path) -> None:
    async def external(*_args, **_kwargs):
        return {"installed": True, "executable": "ollama.exe", "source": "path", "api_healthy": True, "version": "1", "base_url": "http://127.0.0.1:11434", "listener_pid": 42, "latency_ms": 1, "error": None}

    manager = OllamaServiceManager(state_path=tmp_path / "state.json")
    monkeypatch.setattr("app.local_runtime.ollama_service_manager.probe_ollama", external)
    with pytest.raises(OllamaServiceError) as raised:
        asyncio.run(manager.stop())
    assert raised.value.code == "EXTERNAL_PROCESS_PROTECTED"


def test_port_conflict_is_reported_without_starting_process(monkeypatch, tmp_path: Path) -> None:
    async def conflict(*_args, **_kwargs):
        return {"installed": True, "executable": "ollama.exe", "source": "path", "api_healthy": False, "version": None, "base_url": "http://127.0.0.1:11434", "listener_pid": 9, "latency_ms": 1, "error": "PORT_CONFLICT"}

    manager = OllamaServiceManager(state_path=tmp_path / "state.json")
    monkeypatch.setattr("app.local_runtime.ollama_service_manager.probe_ollama", conflict)
    with pytest.raises(OllamaServiceError) as raised:
        asyncio.run(manager.start())
    assert raised.value.code == "PORT_CONFLICT"


def test_model_download_requires_confirmation() -> None:
    manager = ModelManager("http://127.0.0.1:11435")
    with pytest.raises(ModelManagerError) as raised:
        asyncio.run(manager.start_download("new-model:latest", confirmed=False))
    assert raised.value.code == "DOWNLOAD_CONFIRMATION_REQUIRED"


def test_keep_alive_is_bounded() -> None:
    manager = ModelManager("http://127.0.0.1:11435")
    with pytest.raises(ModelManagerError) as raised:
        asyncio.run(manager.preload("qwen3:4b", "forever"))
    assert raised.value.code == "INVALID_KEEP_ALIVE"


def test_unload_refuses_active_generation(monkeypatch) -> None:
    manager = ModelManager("http://127.0.0.1:11435")
    monkeypatch.setattr("app.providers.ollama.active_ollama_requests", lambda model=None: 1)
    with pytest.raises(ModelManagerError) as raised:
        asyncio.run(manager.unload("qwen3:4b"))
    assert raised.value.code == "ACTIVE_GENERATION"
