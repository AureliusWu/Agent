from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import threading
from pathlib import Path

import pytest

from app.local_runtime.model_manager import ModelManager
from app.local_runtime.ollama_service_manager import OllamaServiceError, OllamaServiceManager


pytestmark = pytest.mark.local_model


_GENERIC_LIFECYCLE_SELECTED = pytest.mark.skipif(
    os.getenv("SIYI_TEST_OLLAMA_LIFECYCLE") != "1",
    reason="requires explicit real Ollama lifecycle selection",
)
_QWEN_LIFECYCLE_SELECTION = "managed-qwen3:4b"
_QWEN_LIFECYCLE_SELECTED = pytest.mark.skipif(
    os.getenv("SIYI_TEST_OLLAMA_QWEN3_4B_LIFECYCLE") != _QWEN_LIFECYCLE_SELECTION,
    reason=(
        "requires SIYI_TEST_OLLAMA_QWEN3_4B_LIFECYCLE=managed-qwen3:4b; "
        "this test starts and stops only a test-owned 11435 qwen3:4b lifecycle"
    ),
)
_QWEN_MODEL = "qwen3:4b"
_MANAGED_OLLAMA_URL = "http://127.0.0.1:11435"
_OWNER_ENV = "SIYI_TEST_OLLAMA_OWNER_TOKEN"
_MODEL_STORE_ENV = "SIYI_TEST_OLLAMA_MODEL_STORE"


def _model_is_running(models: list[dict[str, object]], model: str) -> bool:
    return any(str(item.get("name") or item.get("model") or "") == model for item in models)


def _resource_release_observed(release: dict[str, object]) -> bool:
    """At least one observed test-owned Ollama resource must be returned after unload."""

    return any(isinstance(value, int) and value > 0 for value in release.values())


def _test_owner_token() -> str:
    owner = os.getenv(_OWNER_ENV, "").strip()
    assert len(owner) >= 16, f"{_OWNER_ENV} must contain a 16+ character test-owned token"
    return owner


def _read_only_model_store() -> Path:
    value = os.getenv(_MODEL_STORE_ENV, "").strip()
    assert value, f"{_MODEL_STORE_ENV} must explicitly name the existing read-only model-store dependency"
    root = Path(value).expanduser().resolve()
    assert root.is_dir() and (root / "manifests").is_dir(), "explicit Ollama model store is unavailable"
    return root


def _manifest_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    count = 0
    for item in sorted((root / "manifests").rglob("*")):
        if not item.is_file():
            continue
        digest.update(item.relative_to(root / "manifests").as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\n")
        count += 1
    assert count > 0, "explicit Ollama model store has no manifests"
    return digest.hexdigest()


async def _assert_owned_running(manager: OllamaServiceManager, owner: str) -> dict[str, object]:
    status = await manager.status()
    assert status["status"] == "MANAGED_RUNNING", status
    assert status["mode"] == "managed", status
    assert status["owner_sha256"] == hashlib.sha256(owner.encode("utf-8")).hexdigest(), status
    assert status["managed_pid"] == status["listener_pid"], status
    assert status["managed_port"] == 11435, status
    assert owner not in str(status), "status must never reveal the raw owner token"
    return status


@_GENERIC_LIFECYCLE_SELECTED
def test_test_owned_managed_lifecycle_isolated(tmp_path: Path) -> None:
    """Start/stop only a bound 11435 service; port 11434 is never touched."""

    async def scenario() -> None:
        owner = _test_owner_token()
        store = _read_only_model_store()
        manager = OllamaServiceManager(
            base_url=_MANAGED_OLLAMA_URL,
            state_path=tmp_path / "managed.json",
            owner_token=owner,
            require_owner=True,
        )
        before = await manager.status()
        assert before["status"] == "INSTALLED_STOPPED", before
        stopped: dict[str, object] | None = None
        try:
            started = await manager.start(timeout_seconds=20, model_store=store, read_only_model_store=True)
            assert started["started"] is True, started
            first = await _assert_owned_running(manager, owner)
            duplicate = await manager.start(timeout_seconds=5, model_store=store, read_only_model_store=True)
            assert duplicate["managed_pid"] == first["managed_pid"]
        finally:
            if (await manager.status()).get("status") == "MANAGED_RUNNING":
                stopped = await manager.stop()
        assert stopped is not None and stopped["stopped"] is True

    asyncio.run(scenario())


@_GENERIC_LIFECYCLE_SELECTED
def test_real_port_conflict_does_not_terminate_unknown_listener(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.listen(1)
    stopped = threading.Event()

    def serve_unknown() -> None:
        listener.settimeout(0.2)
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stopped.is_set():
                    break
                raise
            with connection:
                connection.recv(4096)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 7\r\n\r\nunknown")

    thread = threading.Thread(target=serve_unknown, daemon=True)
    thread.start()
    manager = OllamaServiceManager(
        base_url=f"http://127.0.0.1:{port}",
        state_path=tmp_path / "conflict.json",
        owner_token="a20-test-owned-ollama-identity-001",
        require_owner=True,
    )
    with pytest.raises(OllamaServiceError) as raised:
        asyncio.run(manager.start(timeout_seconds=2))
    assert raised.value.code == "PORT_CONFLICT"
    assert listener.fileno() >= 0
    stopped.set()
    listener.close()
    thread.join(timeout=1)


@_QWEN_LIFECYCLE_SELECTED
def test_managed_qwen3_4b_preload_unload_lifecycle(tmp_path: Path) -> None:
    """Exercise qwen only inside an identity-proven test-owned 11435 service.

    The service receives an explicit pre-existing model store as a read-only
    dependency.  This test does not pull, copy, delete or clear models.  It
    refuses a non-idle/occupied test port and never sends model commands to
    the user's external 11434 service.
    """

    async def scenario() -> None:
        owner = _test_owner_token()
        store = _read_only_model_store()
        before_store = _manifest_fingerprint(store)
        service = OllamaServiceManager(
            base_url=_MANAGED_OLLAMA_URL,
            state_path=tmp_path / "managed-qwen.json",
            owner_token=owner,
            require_owner=True,
        )
        before = await service.status()
        assert before["status"] == "INSTALLED_STOPPED", before
        manager = ModelManager(_MANAGED_OLLAMA_URL)
        loaded: dict[str, object] | None = None
        unloaded: dict[str, object] | None = None
        unloaded_confirmed = False
        stopped: dict[str, object] | None = None
        try:
            started = await service.start(timeout_seconds=30, model_store=store, read_only_model_store=True)
            assert started["started"] is True, started
            await _assert_owned_running(service, owner)
            running_before = await manager.running_models()
            assert not running_before, {
                "reason": "Refusing lifecycle probe because the test-owned /api/ps is not empty",
                "running_models": running_before,
            }
            installed = await manager.list_models()
            assert any(item["name"] == _QWEN_MODEL for item in installed), {
                "reason": "qwen3:4b is not installed in the explicit read-only dependency; this test never pulls models",
                "installed_models": [item["name"] for item in installed],
            }

            loaded = await manager.preload(_QWEN_MODEL, keep_alive="5m", require_idle_runtime=True)
            assert loaded["status"] == "LOADED", loaded
            assert _model_is_running(await manager.running_models(), _QWEN_MODEL)
            await _assert_owned_running(service, owner)
        finally:
            # A model unload is permitted only after re-proving the same
            # owner/PID/creation/executable/port binding.  If that proof is
            # lost, leave the model untouched and fail the test rather than
            # targeting a same-named user model.
            owned = await service.status()
            if loaded is not None and owned.get("status") == "MANAGED_RUNNING":
                unloaded = await manager.unload(_QWEN_MODEL)
                unloaded_confirmed = not _model_is_running(await manager.running_models(), _QWEN_MODEL)
            if (await service.status()).get("status") == "MANAGED_RUNNING":
                stopped = await service.stop()

        assert loaded is not None
        assert unloaded is not None, "qwen unload requires a preserved test-owned service binding"
        assert unloaded["status"] == "UNLOADED", unloaded
        assert unloaded_confirmed is True
        release = unloaded["resource_release_observed"]
        assert isinstance(release, dict)
        assert _resource_release_observed(release), release
        assert stopped is not None and stopped["stopped"] is True
        assert _manifest_fingerprint(store) == before_store, "test-owned service changed the read-only model-store manifests"

    asyncio.run(scenario())
