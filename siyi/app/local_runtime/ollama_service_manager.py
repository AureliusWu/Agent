from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from app.process_supervisor import _process_identity, terminate_process_tree
from app.runtime_paths import ensure_runtime_layout, runtime_layout

from .ollama_discovery import DEFAULT_OLLAMA_URL, discover_ollama, probe_ollama


class OllamaServiceError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class OllamaServiceManager:
    def __init__(self, *, base_url: str = DEFAULT_OLLAMA_URL, state_path: Path | None = None) -> None:
        layout = ensure_runtime_layout(runtime_layout())
        self.base_url = base_url.rstrip("/")
        self.state_path = state_path or layout.state / "ollama-managed.json"
        self.log_path = layout.logs / "ollama-managed.log"
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = RLock()

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _managed_record(self) -> dict[str, Any] | None:
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        expected = str(state.get("process_identity") or "")
        if pid <= 0 or not expected or _process_identity(pid) != expected:
            return None
        return state

    async def status(self) -> dict[str, Any]:
        probe = await probe_ollama(self.base_url)
        recorded = self._read_state()
        managed = self._managed_record()
        stale_recovered = bool(recorded) and managed is None
        if stale_recovered:
            self.state_path.unlink(missing_ok=True)
        if probe["api_healthy"]:
            mode = "MANAGED_RUNNING" if managed and int(probe.get("listener_pid") or 0) == int(managed["pid"]) else "EXTERNAL_RUNNING"
        elif probe.get("error") == "PORT_CONFLICT":
            mode = "PORT_CONFLICT"
        elif not probe["installed"]:
            mode = "NOT_INSTALLED"
        else:
            mode = "INSTALLED_STOPPED"
        return {
            **probe,
            "status": mode,
            "mode": "managed" if mode.startswith("MANAGED") else ("external" if mode == "EXTERNAL_RUNNING" else None),
            "managed_pid": int(managed["pid"]) if managed else None,
            "managed_started_at": managed.get("started_at") if managed else None,
            "managed_executable": managed.get("executable") if managed else None,
            "executable_sha256": managed.get("executable_sha256") if managed else None,
            "log_path": str(self.log_path) if managed else None,
            "stale_managed_state_recovered": stale_recovered,
        }

    async def start(self, *, executable: str | None = None, timeout_seconds: float = 15.0) -> dict[str, Any]:
        with self._lock:
            existing = self._managed_record()
            if existing:
                return await self.status()
        before = await probe_ollama(self.base_url)
        if before["api_healthy"]:
            return {**(await self.status()), "started": False, "reason": "external_service_already_running"}
        if before.get("error") == "PORT_CONFLICT":
            raise OllamaServiceError("Port 11434 is occupied by a non-Ollama process; it was not terminated", "PORT_CONFLICT")
        installation = discover_ollama(executable)
        if not installation.installed or not installation.executable:
            raise OllamaServiceError("Ollama executable was not found", "NOT_INSTALLED")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = self.log_path.open("ab", buffering=0)
        environment = dict(os.environ)
        parsed = urlsplit(self.base_url)
        environment["OLLAMA_HOST"] = f"127.0.0.1:{parsed.port or 11434}"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(
                [installation.executable, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=environment,
                creationflags=flags,
            )
        except OSError as exc:
            log_handle.close()
            raise OllamaServiceError(f"Unable to start Ollama: {type(exc).__name__}", "START_FAILED") from exc
        identity = _process_identity(process.pid)
        if not identity:
            process.kill()
            log_handle.close()
            raise OllamaServiceError("Unable to record Ollama process identity", "START_FAILED")
        state = {
            "schema_version": 1,
            "manager_id": uuid.uuid4().hex,
            "pid": process.pid,
            "process_identity": identity,
            "executable": str(Path(installation.executable).resolve()),
            "executable_sha256": _sha256(Path(installation.executable)),
            "started_at": time.time(),
            "base_url": self.base_url,
            "stop_on_exit": True,
        }
        self._process = process
        self._write_state(state)
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.state_path.unlink(missing_ok=True)
                log_handle.close()
                raise OllamaServiceError("Managed Ollama exited before health check passed", "EARLY_EXIT")
            current = await probe_ollama(self.base_url, timeout_seconds=1.0)
            if current["api_healthy"]:
                log_handle.close()
                return {**(await self.status()), "started": True}
            await asyncio.sleep(0.2)
        terminate_process_tree(process.pid, identity)
        self.state_path.unlink(missing_ok=True)
        log_handle.close()
        raise OllamaServiceError("Managed Ollama health check timed out", "START_TIMEOUT")

    async def stop(self) -> dict[str, Any]:
        with self._lock:
            state = self._managed_record()
            if not state:
                current = await self.status()
                if current.get("api_healthy"):
                    raise OllamaServiceError("The running Ollama service is external and will not be terminated", "EXTERNAL_PROCESS_PROTECTED")
                self.state_path.unlink(missing_ok=True)
                return {**current, "stopped": False, "reason": "no_managed_service"}
            pid = int(state["pid"])
            identity = str(state["process_identity"])
            if not terminate_process_tree(pid, identity):
                raise OllamaServiceError("Managed Ollama process identity changed; refusing to terminate it", "PROCESS_IDENTITY_MISMATCH")
            self.state_path.unlink(missing_ok=True)
            self._process = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not (await probe_ollama(self.base_url, timeout_seconds=0.5))["api_healthy"]:
                break
            await asyncio.sleep(0.1)
        return {**(await self.status()), "stopped": True, "stopped_pid": pid}

    async def shutdown(self) -> None:
        state = self._managed_record()
        if state and bool(state.get("stop_on_exit", True)):
            try:
                await self.stop()
            except OllamaServiceError:
                pass


_MANAGER: OllamaServiceManager | None = None


def ollama_service_manager() -> OllamaServiceManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = OllamaServiceManager()
    return _MANAGER


def _sha256(path: Path) -> str:
    digest = __import__("hashlib").sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
