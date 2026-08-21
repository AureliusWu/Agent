from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.local_runtime.model_manager import ModelManager, ModelManagerError
from app.local_runtime.ollama_discovery import discover_ollama, validate_local_ollama_url
from app.local_runtime.ollama_service_manager import OllamaServiceError, OllamaServiceManager
from app.local_runtime.resource_coordinator import ResourceCoordinator


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


def test_managed_state_requires_matching_owner_pid_creation_executable_and_port(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "ollama.exe"
    executable.write_bytes(b"fixture")
    state_path = tmp_path / "managed.json"
    owner = "a20-test-owned-ollama-identity-001"
    manager = OllamaServiceManager(
        base_url="http://127.0.0.1:11435",
        state_path=state_path,
        owner_token=owner,
        require_owner=True,
    )
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "owner_sha256": manager.owner_sha256,
                "pid": 73,
                "process_identity": "win:created",
                "executable": str(executable.resolve()),
                "executable_sha256": __import__("hashlib").sha256(b"fixture").hexdigest(),
                "base_url": "http://127.0.0.1:11435",
                "port": 11435,
                "started_at": 1,
            }
        ),
        encoding="utf-8",
    )

    async def healthy(*_args, **_kwargs):
        return {
            "installed": True,
            "api_healthy": True,
            "version": "1",
            "base_url": "http://127.0.0.1:11435",
            "listener_pid": 73,
            "latency_ms": 1,
            "error": None,
        }

    monkeypatch.setattr("app.local_runtime.ollama_service_manager.probe_ollama", healthy)
    monkeypatch.setattr("app.local_runtime.ollama_service_manager._process_identity", lambda pid: "win:created")
    monkeypatch.setattr("app.local_runtime.ollama_service_manager._process_executable_path", lambda pid: executable.resolve())

    status = asyncio.run(manager.status())
    assert status["status"] == "MANAGED_RUNNING"
    assert status["owner_sha256"] == manager.owner_sha256
    assert status["managed_port"] == 11435
    assert "managed_executable" not in status
    assert "log_path" not in status

    wrong_owner = OllamaServiceManager(
        base_url="http://127.0.0.1:11435",
        state_path=state_path,
        owner_token="a20-test-owned-ollama-identity-other",
        require_owner=True,
    )
    unowned = asyncio.run(wrong_owner.status())
    assert unowned["status"] == "EXTERNAL_RUNNING"
    assert unowned["managed_state_unowned"] is True
    with pytest.raises(OllamaServiceError) as protected:
        asyncio.run(wrong_owner.stop())
    assert protected.value.code == "EXTERNAL_PROCESS_PROTECTED"

    stored = json.loads(state_path.read_text(encoding="utf-8"))
    stored["port"] = 11434
    state_path.write_text(json.dumps(stored), encoding="utf-8")
    mismatched = asyncio.run(manager.status())
    assert mismatched["status"] == "EXTERNAL_RUNNING"
    assert mismatched["managed_pid"] is None


def test_strict_manager_refuses_start_without_an_explicit_owner(tmp_path: Path) -> None:
    manager = OllamaServiceManager(
        base_url="http://127.0.0.1:11435",
        state_path=tmp_path / "state.json",
        require_owner=True,
    )
    with pytest.raises(OllamaServiceError) as raised:
        manager._ensure_owner()
    assert raised.value.code == "OWNER_REQUIRED"


def test_resource_snapshot_uses_only_explicit_ollama_listener_pid(monkeypatch) -> None:
    observed_names: list[str] = []

    def named_rss(name: str) -> int | None:
        observed_names.append(name)
        return 10

    monkeypatch.setattr("app.local_runtime.resource_coordinator._process_rss", named_rss)
    rss_by_pid = {73: 700, 80: 120, 81: 130}
    monkeypatch.setattr(
        "app.local_runtime.resource_coordinator._process_rss_by_pid",
        lambda pid: rss_by_pid.get(pid),
    )
    monkeypatch.setattr("app.local_runtime.resource_coordinator._memory", lambda: (1000, 900))
    monkeypatch.setattr("app.local_runtime.resource_coordinator._gpu", lambda: (None, None))
    coordinator = ResourceCoordinator()

    targeted = coordinator.snapshot(ollama_pid=73, tts_pids=[81, 80, 81])
    unobserved = coordinator.snapshot()

    assert targeted["ollama_pid"] == 73
    assert targeted["ollama_rss_bytes"] == 700
    assert targeted["tts_pids"] == (80, 81)
    assert targeted["tts_rss_bytes"] == 250
    assert unobserved["ollama_pid"] is None
    assert unobserved["ollama_rss_bytes"] is None
    assert unobserved["tts_pids"] == ()
    assert unobserved["tts_rss_bytes"] is None
    assert "ollama" not in observed_names
    assert "powershell" not in observed_names


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


def test_preload_can_require_an_idle_external_runtime(monkeypatch) -> None:
    """The live lifecycle probe must never evict someone else's model."""

    manager = ModelManager("http://127.0.0.1:11435")
    calls: list[str] = []

    async def running_models() -> list[dict[str, str]]:
        calls.append("running")
        return [{"name": "someone-elses-model:latest"}]

    async def unload(model: str) -> dict[str, str]:
        calls.append(f"unload:{model}")
        return {"status": "UNLOADED"}

    async def request(*_args, **_kwargs) -> dict:
        calls.append("request")
        return {}

    monkeypatch.setattr(manager, "running_models", running_models)
    monkeypatch.setattr(manager, "unload", unload)
    monkeypatch.setattr(manager, "_json", request)

    with pytest.raises(ModelManagerError) as raised:
        asyncio.run(manager.preload("qwen3:4b", require_idle_runtime=True))

    assert raised.value.code == "OLLAMA_RUNTIME_NOT_IDLE"
    assert calls == ["running"]


def test_preload_refuses_resource_pressure_before_unloading_another_model(monkeypatch) -> None:
    """Admission must not evict a known model merely to attempt a preload."""
    manager = ModelManager("http://127.0.0.1:11435")
    coordinator = ResourceCoordinator(minimum_available_ram_bytes=100, minimum_free_vram_bytes=0)
    calls: list[str] = []
    monkeypatch.setattr("app.local_runtime.model_manager.resource_coordinator", coordinator)
    monkeypatch.setattr("app.local_runtime.resource_coordinator._memory", lambda: (1_000, 99))
    monkeypatch.setattr("app.local_runtime.resource_coordinator._gpu", lambda: (None, None))

    async def running_models() -> list[dict[str, str]]:
        calls.append("running")
        return [{"name": "already-loaded:latest"}]

    async def unexpected_unload(_model: str) -> dict[str, str]:
        calls.append("unload")
        return {"status": "UNLOADED"}

    async def unexpected_request(*_args, **_kwargs) -> dict:
        calls.append("request")
        return {}

    monkeypatch.setattr(manager, "running_models", running_models)
    monkeypatch.setattr(manager, "unload", unexpected_unload)
    monkeypatch.setattr(manager, "_json", unexpected_request)

    with pytest.raises(ModelManagerError) as raised:
        asyncio.run(manager.preload("qwen3:4b"))

    assert raised.value.code == "RESOURCE_RAM_PRESSURE"
    assert calls == ["running"]
