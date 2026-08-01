from __future__ import annotations

import csv
import io
import os
import subprocess
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ResourceSnapshot:
    system_total_bytes: int | None
    system_available_bytes: int | None
    gpu_total_bytes: int | None
    gpu_free_bytes: int | None
    backend_rss_bytes: int | None
    ollama_rss_bytes: int | None
    tts_rss_bytes: int | None
    active_model: str | None
    tts_provider: str | None


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


def _memory() -> tuple[int | None, int | None]:
    payload = _powershell_json(
        "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json -Compress"
    )
    if not isinstance(payload, dict):
        return None, None
    return int(payload["TotalVisibleMemorySize"]) * 1024, int(payload["FreePhysicalMemory"]) * 1024


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

    def snapshot(self, *, active_model: str | None = None, tts_provider: str | None = None) -> dict:
        total, available = _memory()
        gpu_total, gpu_free = _gpu()
        return asdict(
            ResourceSnapshot(
                system_total_bytes=total,
                system_available_bytes=available,
                gpu_total_bytes=gpu_total,
                gpu_free_bytes=gpu_free,
                backend_rss_bytes=_process_rss("agent-backend") or _process_rss("python"),
                ollama_rss_bytes=_process_rss("ollama"),
                tts_rss_bytes=_process_rss("powershell"),
                active_model=active_model,
                tts_provider=tts_provider,
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
        }


resource_coordinator = ResourceCoordinator()
