from __future__ import annotations

import csv
import ctypes
import io
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


_GIB = 1024 * 1024 * 1024
_MIB = 1024 * 1024


def _configured_non_negative_bytes(name: str, default: int) -> int:
    """Read one safe resource threshold without making bad env input fatal."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ResourceSnapshot:
    sampled_at: str
    system_total_bytes: int | None
    system_available_bytes: int | None
    gpu_total_bytes: int | None
    gpu_free_bytes: int | None
    backend_rss_bytes: int | None
    ollama_rss_bytes: int | None
    ollama_pid: int | None
    tts_rss_bytes: int | None
    tts_pids: tuple[int, ...]
    stt_rss_bytes: int | None
    stt_worker_pid: int | None
    active_model: str | None
    tts_provider: str | None
    stt_provider: str | None
    active_voice_sessions: int
    active_stt_requests: int
    recording_active: bool


@dataclass(frozen=True)
class ResourceAdmission:
    """A read-only admission decision for new local resource work.

    It deliberately only describes whether a new operation may start.  It
    never unloads a model, interrupts an existing task, or acts on processes
    which the application does not own.
    """

    workload: str
    allowed: bool
    reason_code: str | None
    reason_codes: tuple[str, ...]
    reason: str | None
    requires_gpu: bool
    system_memory_observed: bool
    gpu_memory_observed: bool
    system_available_bytes: int | None
    gpu_free_bytes: int | None
    minimum_available_ram_bytes: int
    minimum_free_vram_bytes: int | None
    sampled_at: str | None


def resource_pressure_details(admission: Mapping[str, Any]) -> dict[str, int | str] | None:
    """Return the small, safe subset needed to explain a denied admission.

    The full runtime snapshot can contain process and model metadata.  Error
    responses only need the observed and required byte counts, so callers pass
    this allowlisted shape through the exception boundary instead.
    """
    code = str(admission.get("reason_code") or "")
    if code == "RESOURCE_RAM_PRESSURE":
        kind = "ram"
        available = _optional_int(admission.get("system_available_bytes"))
        minimum = _optional_int(admission.get("minimum_available_ram_bytes"))
    elif code == "RESOURCE_VRAM_PRESSURE":
        kind = "vram"
        available = _optional_int(admission.get("gpu_free_bytes"))
        minimum = _optional_int(admission.get("minimum_free_vram_bytes"))
    else:
        return None
    if available is None or minimum is None or available < 0 or minimum < 0:
        return None
    return {
        "kind": kind,
        "available_bytes": available,
        "minimum_available_bytes": minimum,
    }


def _powershell_json(script: str) -> dict | list | None:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return __import__("json").loads(result.stdout)
    except ValueError:
        return None


def _process_rss(name: str) -> int | None:
    payload = _powershell_json(
        f"@(Get-Process -Name '{name}' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty WorkingSet64) | ConvertTo-Json -Compress"
    )
    values = payload if isinstance(payload, list) else ([payload] if isinstance(payload, int) else [])
    return sum(int(item) for item in values) if values else None


def _process_rss_by_pid(pid: int | None) -> int | None:
    """Return the RSS of one known child process, never a name-based estimate.

    A Faster-Whisper worker is a separate supervised process.  Looking it up by
    PID avoids treating every Python process on the machine as STT memory and
    gives the resource panel a real unload/release signal.
    """
    if pid is None or pid <= 0:
        return None
    payload = _powershell_json(
        f"Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty WorkingSet64 | ConvertTo-Json -Compress"
    )
    return int(payload) if isinstance(payload, int) else None


def _process_rss_by_pids(pids: Iterable[int]) -> int | None:
    values = [value for pid in pids if (value := _process_rss_by_pid(pid)) is not None]
    return sum(values) if values else None


def _memory() -> tuple[int | None, int | None]:
    """Return physical total and Windows *available* RAM, not only free pages.

    ``Win32_OperatingSystem.FreePhysicalMemory`` excludes reclaimable cache and
    standby pages, which makes it unsuitable as a pressure signal.  The Win32
    API's ``ullAvailPhys`` is the same availability concept exposed to users
    by Windows resource tools.
    """
    if os.name != "nt":
        return None, None

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        success = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None, None
    if not success:
        return None, None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _gpu() -> tuple[int | None, int | None]:
    executable = __import__("shutil").which("nvidia-smi")
    if not executable:
        return None, None
    result = subprocess.run(
        [executable, "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        return None, None
    row = next(csv.reader(io.StringIO(result.stdout)), None)
    if not row or len(row) < 2:
        return None, None
    return int(row[0].strip()) * 1024 * 1024, int(row[1].strip()) * 1024 * 1024


class ResourceCoordinator:
    max_loaded_models = 1
    max_parallel_inference = 1
    default_context = 4096
    default_keep_alive = "5m"
    max_voice_sessions = 1
    max_stt_requests = 1

    def __init__(
        self,
        *,
        minimum_available_ram_bytes: int | None = None,
        minimum_free_vram_bytes: int | None = None,
    ) -> None:
        # A reservation is owned by a concrete session/request.  This makes
        # release idempotent across a stop-vs-completion race: one session can
        # never decrement another session's resource accounting.
        self._voice_reservations: set[str] = set()
        self._stt_reservations: set[str] = set()
        # These defaults leave headroom on the documented 16 GB / 6 GB target
        # without forcing a GPU requirement for the CPU-first voice path.
        # Environment overrides are bytes, so the packaged app and automated
        # verification can tune them without changing source code.
        self.minimum_available_ram_bytes = (
            _configured_non_negative_bytes("SIYI_RESOURCE_MIN_AVAILABLE_RAM_BYTES", 2 * _GIB)
            if minimum_available_ram_bytes is None
            else max(0, int(minimum_available_ram_bytes))
        )
        self.minimum_free_vram_bytes = (
            _configured_non_negative_bytes("SIYI_RESOURCE_MIN_FREE_VRAM_BYTES", 512 * _MIB)
            if minimum_free_vram_bytes is None
            else max(0, int(minimum_free_vram_bytes))
        )

    def assess_admission(
        self,
        workload: str,
        *,
        requires_gpu: bool = False,
        snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return an observable, non-destructive admission decision.

        ``voice`` and default ``stt`` use CPU and therefore do not fail only
        because GPU telemetry is low or unavailable.  A GPU STT request and a
        local-model preload opt into the VRAM check.  Missing telemetry is
        reported rather than guessed; it is not treated as permission to kill
        a process or as a reason to disable text input.
        """
        if workload not in {"voice", "stt", "model_preload"}:
            raise ValueError(f"Unknown resource workload: {workload}")

        observed = dict(snapshot) if snapshot is not None else self.snapshot()
        available = _optional_int(observed.get("system_available_bytes"))
        gpu_free = _optional_int(observed.get("gpu_free_bytes"))
        reason_codes: list[str] = []
        details: list[str] = []

        if available is not None and available < self.minimum_available_ram_bytes:
            reason_codes.append("RESOURCE_RAM_PRESSURE")
            details.append(
                "available RAM "
                f"{available} bytes is below the safe threshold {self.minimum_available_ram_bytes} bytes"
            )
        if requires_gpu and gpu_free is not None and gpu_free < self.minimum_free_vram_bytes:
            reason_codes.append("RESOURCE_VRAM_PRESSURE")
            details.append(
                "free VRAM "
                f"{gpu_free} bytes is below the safe threshold {self.minimum_free_vram_bytes} bytes"
            )

        return asdict(
            ResourceAdmission(
                workload=workload,
                allowed=not reason_codes,
                reason_code=reason_codes[0] if reason_codes else None,
                reason_codes=tuple(reason_codes),
                reason="; ".join(details) if details else None,
                requires_gpu=requires_gpu,
                system_memory_observed=available is not None,
                gpu_memory_observed=gpu_free is not None,
                system_available_bytes=available,
                gpu_free_bytes=gpu_free,
                minimum_available_ram_bytes=self.minimum_available_ram_bytes,
                minimum_free_vram_bytes=self.minimum_free_vram_bytes if requires_gpu else None,
                sampled_at=observed.get("sampled_at") if isinstance(observed.get("sampled_at"), str) else None,
            )
        )

    def admission_status(self, *, snapshot: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """Expose the exact checks used by new work without starting it."""
        return {
            "voice": self.assess_admission("voice", snapshot=snapshot),
            "stt_cpu": self.assess_admission("stt", snapshot=snapshot),
            "stt_gpu": self.assess_admission("stt", requires_gpu=True, snapshot=snapshot),
            "model_preload": self.assess_admission("model_preload", requires_gpu=True, snapshot=snapshot),
        }

    def acquire_voice_session(self, voice_session_id: str) -> bool:
        if voice_session_id in self._voice_reservations:
            return True
        if len(self._voice_reservations) >= self.max_voice_sessions:
            return False
        self._voice_reservations.add(voice_session_id)
        return True

    def release_voice_session(self, voice_session_id: str) -> None:
        self._voice_reservations.discard(voice_session_id)

    def acquire_stt(self, request_id: str) -> bool:
        if request_id in self._stt_reservations:
            return True
        if len(self._stt_reservations) >= self.max_stt_requests:
            return False
        self._stt_reservations.add(request_id)
        return True

    def release_stt(self, request_id: str) -> None:
        self._stt_reservations.discard(request_id)

    @property
    def recording_active(self) -> bool:
        return bool(self._voice_reservations)

    def snapshot(
        self,
        *,
        active_model: str | None = None,
        tts_provider: str | None = None,
        stt_provider: str | None = None,
        stt_worker_pid: int | None = None,
        ollama_pid: int | None = None,
        tts_pids: Iterable[int] = (),
    ) -> dict:
        total, available = _memory()
        sampled_at = datetime.now(timezone.utc).isoformat()
        gpu_total, gpu_free = _gpu()
        observed_ollama_pid = ollama_pid if isinstance(ollama_pid, int) and not isinstance(ollama_pid, bool) and ollama_pid > 0 else None
        observed_tts_pids = tuple(
            sorted(
                {
                    int(pid)
                    for pid in tts_pids
                    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
                }
            )
        )
        return asdict(
            ResourceSnapshot(
                sampled_at=sampled_at,
                system_total_bytes=total,
                system_available_bytes=available,
                gpu_total_bytes=gpu_total,
                gpu_free_bytes=gpu_free,
                backend_rss_bytes=_process_rss("agent-backend") or _process_rss("python"),
                # Ollama may be a user-owned external service.  Sampling all
                # processes named ollama.exe would merge unrelated models and
                # create a false unload/release signal.  Report only an exact
                # caller-supplied listener PID; otherwise leave it unobserved.
                ollama_rss_bytes=_process_rss_by_pid(observed_ollama_pid) if observed_ollama_pid is not None else None,
                ollama_pid=observed_ollama_pid,
                # Windows TTS owns exact supervised PowerShell children.
                # Never attribute unrelated user PowerShell processes to TTS.
                tts_rss_bytes=_process_rss_by_pids(observed_tts_pids),
                tts_pids=observed_tts_pids,
                # The STT worker is a dedicated child.  Its PID is supplied by
                # STTManager, so this is an observed per-process value rather
                # than an invented slice of the whole sidecar RSS.
                stt_rss_bytes=_process_rss_by_pid(stt_worker_pid),
                stt_worker_pid=stt_worker_pid,
                active_model=active_model,
                tts_provider=tts_provider,
                stt_provider=stt_provider,
                active_voice_sessions=len(self._voice_reservations),
                active_stt_requests=len(self._stt_reservations),
                recording_active=bool(self._voice_reservations),
            )
        )

    def policy(self) -> dict:
        return {
            "max_loaded_models": self.max_loaded_models,
            "max_parallel_inference": self.max_parallel_inference,
            "default_context": self.default_context,
            "default_keep_alive": self.default_keep_alive,
            "automatic_paid_fallback": False,
            "tts_device": "cpu",
            "stt_device": "cpu",
            "stt_compute_type": "int8",
            "max_voice_sessions": self.max_voice_sessions,
            "max_stt_requests": self.max_stt_requests,
            "minimum_available_ram_bytes": self.minimum_available_ram_bytes,
            "minimum_free_vram_bytes": self.minimum_free_vram_bytes,
            "text_input_available_under_pressure": True,
            "unknown_processes_are_never_terminated": True,
        }


resource_coordinator = ResourceCoordinator()
