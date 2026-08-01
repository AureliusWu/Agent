from __future__ import annotations

import asyncio
import os
import socket
import threading
from pathlib import Path

import pytest

from app.local_runtime.ollama_service_manager import OllamaServiceError, OllamaServiceManager


pytestmark = [
    pytest.mark.local_model,
    pytest.mark.skipif(os.getenv("SIYI_TEST_OLLAMA_LIFECYCLE") != "1", reason="requires explicit real Ollama lifecycle selection"),
]


def test_external_and_managed_lifecycle_are_isolated(tmp_path: Path) -> None:
    async def scenario() -> None:
        external = OllamaServiceManager(base_url="http://127.0.0.1:11434", state_path=tmp_path / "external.json")
        external_status = await external.status()
        assert external_status["status"] == "EXTERNAL_RUNNING", external_status
        external_pid = external_status["listener_pid"]
        with pytest.raises(OllamaServiceError) as protected:
            await external.stop()
        assert protected.value.code == "EXTERNAL_PROCESS_PROTECTED"

        managed = OllamaServiceManager(base_url="http://127.0.0.1:11435", state_path=tmp_path / "managed.json")
        before = await managed.status()
        assert before["status"] == "INSTALLED_STOPPED", before
        started = await managed.start(timeout_seconds=20)
        assert started["status"] == "MANAGED_RUNNING", started
        assert started["managed_pid"] != external_pid
        duplicate = await managed.start(timeout_seconds=5)
        assert duplicate["managed_pid"] == started["managed_pid"]
        stopped = await managed.stop()
        assert stopped["stopped"] is True
        assert (await external.status())["listener_pid"] == external_pid

    asyncio.run(scenario())


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
    manager = OllamaServiceManager(base_url=f"http://127.0.0.1:{port}", state_path=tmp_path / "conflict.json")
    with pytest.raises(OllamaServiceError) as raised:
        asyncio.run(manager.start(timeout_seconds=2))
    assert raised.value.code == "PORT_CONFLICT"
    assert listener.fileno() >= 0
    stopped.set()
    listener.close()
    thread.join(timeout=1)
