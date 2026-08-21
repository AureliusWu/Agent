from __future__ import annotations

import asyncio
import ctypes
import hashlib
import hmac
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

from .ollama_discovery import DEFAULT_OLLAMA_URL, discover_ollama, probe_ollama, validate_local_ollama_url


class OllamaServiceError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _owner_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _process_executable_path(pid: int) -> Path | None:
    """Return the image path for one live PID without name-based process lookup."""

    if pid <= 0:
        return None
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        try:
            capacity = 32_768
            buffer = ctypes.create_unicode_buffer(capacity)
            length = ctypes.c_ulong(capacity)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return None
            return Path(buffer.value).resolve()
        except OSError:
            return None
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/exe").resolve(strict=True)
    except OSError:
        return None


class OllamaServiceManager:
    """Manage only an Ollama process whose complete ownership binding is known.

    A state file is intentionally insufficient proof on its own.  A managed
    record is usable only when its owner hash, PID creation identity,
    executable image and loopback listener port all still agree.  If any part
    is missing or has changed, the listener is reported as external and is
    never stopped or used as a test-owned lifecycle target.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_URL,
        state_path: Path | None = None,
        owner_token: str | None = None,
        require_owner: bool = False,
    ) -> None:
        layout = ensure_runtime_layout(runtime_layout())
        self.base_url = validate_local_ollama_url(base_url)
        self.state_path = state_path or layout.state / "ollama-managed.json"
        self.log_path = layout.logs / "ollama-managed.log"
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = RLock()
        self._owner_token = owner_token or None
        self._owner_hash = _owner_sha256(owner_token) if owner_token else None
        self.require_owner = bool(require_owner)

    @property
    def owner_sha256(self) -> str | None:
        """Return the opaque owner fingerprint; never expose its raw token."""

        return self._owner_hash

    def _ensure_owner(self) -> None:
        if self._owner_hash:
            return
        if self.require_owner:
            raise OllamaServiceError("Managed Ollama requires an explicit owner token", "OWNER_REQUIRED")
        # Normal desktop lifecycle remains API-compatible: the singleton
        # retains this in-memory owner token for its own start/stop lifetime.
        # It is never written to disk, returned by status, or reused after a
        # sidecar restart, where the old process becomes protected external.
        self._owner_token = uuid.uuid4().hex
        self._owner_hash = _owner_sha256(self._owner_token)

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

    @property
    def _port(self) -> int:
        return urlsplit(self.base_url).port or 11434

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return str(left).casefold() == str(right).casefold() if os.name == "nt" else left == right

    def _binding_record(
        self,
        state: dict[str, Any],
        probe: dict[str, Any],
        *,
        verify_executable_hash: bool = False,
    ) -> dict[str, Any] | None:
        """Return state only when it still names this exact live listener."""

        try:
            pid = int(state.get("pid") or 0)
            port = int(state.get("port") or 0)
            identity = str(state.get("process_identity") or "")
            owner_hash = str(state.get("owner_sha256") or "")
            executable = Path(str(state.get("executable") or "")).resolve(strict=True)
            executable_hash = str(state.get("executable_sha256") or "")
        except (OSError, TypeError, ValueError):
            return None
        if (
            pid <= 0
            or port != self._port
            or str(state.get("base_url") or "") != self.base_url
            or len(owner_hash) != 64
            or not identity
            or len(executable_hash) != 64
            or not probe.get("api_healthy")
            or int(probe.get("listener_pid") or 0) != pid
            or _process_identity(pid) != identity
        ):
            return None
        observed_executable = _process_executable_path(pid)
        if observed_executable is None or not self._same_path(observed_executable, executable):
            return None
        # Status refreshes are intentionally cheap; they still bind PID,
        # creation fingerprint, executable path and listener port.  Before a
        # destructive stop, additionally prove the selected image hash so a
        # replacement binary cannot inherit the old state record.
        if verify_executable_hash:
            try:
                if not hmac.compare_digest(_sha256(executable), executable_hash):
                    return None
            except OSError:
                return None
        return state

    def _owned_record(
        self,
        state: dict[str, Any],
        probe: dict[str, Any],
        *,
        verify_executable_hash: bool = False,
    ) -> dict[str, Any] | None:
        record = self._binding_record(state, probe, verify_executable_hash=verify_executable_hash)
        if record is None or not self._owner_hash:
            return None
        expected = str(record.get("owner_sha256") or "")
        return record if hmac.compare_digest(expected, self._owner_hash) else None

    def _safe_status_fields(self, state: dict[str, Any] | None) -> dict[str, Any]:
        """Expose opaque lifecycle metadata only; paths and owner tokens stay private."""

        if state is None:
            return {
                "managed_pid": None,
                "managed_started_at": None,
                "managed_port": None,
                "owner_sha256": None,
                "executable_sha256": None,
            }
        return {
            "managed_pid": int(state["pid"]),
            "managed_started_at": state.get("started_at"),
            "managed_port": int(state["port"]),
            "owner_sha256": state.get("owner_sha256"),
            "executable_sha256": state.get("executable_sha256"),
        }

    async def status(self) -> dict[str, Any]:
        probe = await probe_ollama(self.base_url)
        recorded = self._read_state()
        bound = self._binding_record(recorded, probe) if recorded else None
        managed = self._owned_record(recorded, probe) if recorded else None
        # An old record without a current matching process is stale.  Do not
        # delete a record while some healthy but unowned listener exists: that
        # may be a process started by another sidecar and must remain protected.
        stale_recovered = bool(recorded) and bound is None and not probe.get("api_healthy")
        if stale_recovered:
            self.state_path.unlink(missing_ok=True)
        if probe["api_healthy"]:
            mode = "MANAGED_RUNNING" if managed else "EXTERNAL_RUNNING"
        elif probe.get("error") == "PORT_CONFLICT":
            mode = "PORT_CONFLICT"
        elif not probe["installed"]:
            mode = "NOT_INSTALLED"
        else:
            mode = "INSTALLED_STOPPED"
        # ``probe_ollama`` contains local executable discovery details.  They
        # are useful internally but must not become API/evidence path leaks.
        public_probe = {
            "installed": bool(probe.get("installed")),
            "api_healthy": bool(probe.get("api_healthy")),
            "version": probe.get("version"),
            "base_url": probe.get("base_url"),
            "listener_pid": probe.get("listener_pid"),
            "latency_ms": probe.get("latency_ms"),
            "error": probe.get("error"),
            "detail": probe.get("detail"),
        }
        return {
            **public_probe,
            "status": mode,
            "mode": "managed" if mode == "MANAGED_RUNNING" else ("external" if mode == "EXTERNAL_RUNNING" else None),
            **self._safe_status_fields(managed),
            "managed_state_unowned": bool(bound and managed is None),
            "stale_managed_state_recovered": stale_recovered,
        }

    async def start(
        self,
        *,
        executable: str | None = None,
        timeout_seconds: float = 15.0,
        model_store: Path | None = None,
        read_only_model_store: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._ensure_owner()
            current_probe = await probe_ollama(self.base_url)
            existing = self._owned_record(self._read_state(), current_probe)
            if existing:
                return await self.status()
        before = current_probe
        if before["api_healthy"]:
            return {**(await self.status()), "started": False, "reason": "external_service_already_running"}
        if before.get("error") == "PORT_CONFLICT":
            raise OllamaServiceError(
                f"Port {self._port} is occupied by a non-Ollama process; it was not terminated", "PORT_CONFLICT"
            )
        installation = discover_ollama(executable)
        if not installation.installed or not installation.executable:
            raise OllamaServiceError("Ollama executable was not found", "NOT_INSTALLED")
        selected_store: Path | None = None
        if model_store is not None:
            try:
                selected_store = model_store.expanduser().resolve(strict=True)
            except OSError as exc:
                raise OllamaServiceError("The explicit Ollama model store is unavailable", "MODEL_STORE_UNAVAILABLE") from exc
            if not selected_store.is_dir():
                raise OllamaServiceError("The explicit Ollama model store is not a directory", "MODEL_STORE_UNAVAILABLE")
        if read_only_model_store and selected_store is None:
            raise OllamaServiceError("Read-only model-store mode requires an explicit existing store", "MODEL_STORE_REQUIRED")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = self.log_path.open("ab", buffering=0)
        environment = dict(os.environ)
        environment["OLLAMA_HOST"] = f"127.0.0.1:{self._port}"
        if selected_store is not None:
            # Lifecycle management never invokes pull/delete.  A selected
            # model store is an existing dependency; it is not created,
            # copied, or cleared by this manager.
            environment["OLLAMA_MODELS"] = str(selected_store)
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
        expected_executable = Path(installation.executable).resolve()
        identity: str | None = None
        actual_executable: Path | None = None
        binding_deadline = time.monotonic() + 2.0
        while time.monotonic() < binding_deadline:
            identity = _process_identity(process.pid)
            actual_executable = _process_executable_path(process.pid)
            if identity is not None and actual_executable is not None and self._same_path(actual_executable, expected_executable):
                break
            if process.poll() is not None:
                break
            await asyncio.sleep(0.05)
        if identity is None or actual_executable is None or not self._same_path(actual_executable, expected_executable):
            # Binding failed before the service was admitted.  Use only the
            # direct Popen handle we just created; do not issue a PID/tree kill
            # against an identity we could not prove.
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            log_handle.close()
            raise OllamaServiceError("Unable to bind the managed Ollama process identity", "START_FAILED")
        state = {
            "schema_version": 2,
            "owner_sha256": self._owner_hash,
            "pid": process.pid,
            "process_identity": identity,
            "executable": str(expected_executable),
            "executable_sha256": _sha256(expected_executable),
            "started_at": time.time(),
            "base_url": self.base_url,
            "port": self._port,
            "stop_on_exit": True,
            "model_store_mode": "read_only_dependency" if read_only_model_store else "default",
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
                if self._owned_record(state, current) is None:
                    terminate_process_tree(process.pid, identity)
                    self.state_path.unlink(missing_ok=True)
                    log_handle.close()
                    raise OllamaServiceError("Ollama listener did not match the managed process binding", "LISTENER_IDENTITY_MISMATCH")
                log_handle.close()
                return {**(await self.status()), "started": True}
            await asyncio.sleep(0.2)
        terminate_process_tree(process.pid, identity)
        self.state_path.unlink(missing_ok=True)
        log_handle.close()
        raise OllamaServiceError("Managed Ollama health check timed out", "START_TIMEOUT")

    async def stop(self) -> dict[str, Any]:
        with self._lock:
            probe = await probe_ollama(self.base_url)
            recorded = self._read_state()
            state = self._owned_record(recorded, probe, verify_executable_hash=True) if recorded else None
            if not state:
                current = await self.status()
                if current.get("api_healthy"):
                    raise OllamaServiceError("The running Ollama service is external and will not be terminated", "EXTERNAL_PROCESS_PROTECTED")
                return {**current, "stopped": False, "reason": "no_owned_managed_service"}
            pid = int(state["pid"])
            identity = str(state["process_identity"])
            if not terminate_process_tree(pid, identity):
                raise OllamaServiceError("Managed Ollama process binding changed; refusing to terminate it", "PROCESS_IDENTITY_MISMATCH")
            self.state_path.unlink(missing_ok=True)
            self._process = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not (await probe_ollama(self.base_url, timeout_seconds=0.5))["api_healthy"]:
                break
            await asyncio.sleep(0.1)
        return {**(await self.status()), "stopped": True, "stopped_pid": pid}

    async def shutdown(self) -> None:
        # If this manager lost its in-memory owner token after a sidecar crash,
        # the old listener is deliberately external rather than reclaimed.
        if self._owner_hash:
            try:
                await self.stop()
            except OllamaServiceError:
                pass


_MANAGER: OllamaServiceManager | None = None


def _configured_runtime_url() -> str:
    return os.environ.get("AGENT_LOCAL_OLLAMA_URL", DEFAULT_OLLAMA_URL).strip() or DEFAULT_OLLAMA_URL


def ollama_service_manager() -> OllamaServiceManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = OllamaServiceManager(base_url=_configured_runtime_url())
    return _MANAGER


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
