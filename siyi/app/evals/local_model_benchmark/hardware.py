from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import HardwareSnapshot


def _total_memory_bytes() -> int | None:
    if os.name == "nt":
        class MemoryStatusEx(ctypes.Structure):
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
            value = MemoryStatusEx()
            value.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
                return int(value.ullTotalPhys)
        except (AttributeError, OSError):
            return None
        return None
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return pages * page_size if pages > 0 and page_size > 0 else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _gpu() -> tuple[str | None, int | None]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None, None
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if result.returncode != 0 or not result.stdout.strip():
        return None, None
    first = result.stdout.splitlines()[0].split(",", 1)
    if not first:
        return None, None
    name = first[0].strip() or None
    try:
        memory = int(first[1].strip()) * 1024 * 1024 if len(first) > 1 else None
    except ValueError:
        memory = None
    return name, memory


def collect_hardware_snapshot() -> HardwareSnapshot:
    gpu_name, gpu_memory = _gpu()
    cpu_name = platform.processor().strip() or platform.machine().strip() or "unknown"
    return HardwareSnapshot(
        os_name=platform.system() or os.name,
        os_release=platform.release() or "unknown",
        os_version=platform.version() or "unknown",
        architecture=platform.machine() or "unknown",
        cpu_name=cpu_name,
        cpu_logical_cores=max(1, os.cpu_count() or 1),
        ram_total_bytes=_total_memory_bytes(),
        gpu_name=gpu_name,
        gpu_memory_total_bytes=gpu_memory,
    )


def process_rss_bytes(pid: int | None) -> int | None:
    if pid is None or pid <= 0:
        return None
    if os.name == "nt":
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        handle = None
        try:
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            query = ctypes.windll.psapi.GetProcessMemoryInfo
            query.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
            query.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x0410, False, int(pid))
            if not handle:
                return None
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(ProcessMemoryCounters)
            ok = query(
                handle,
                ctypes.byref(counters),
                counters.cb,
            )
            return int(counters.WorkingSetSize) if ok else None
        except (AttributeError, OSError):
            return None
        finally:
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
    status = Path(f"/proc/{int(pid)}/status")
    try:
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, IndexError, ValueError):
        return None
    return None


@dataclass
class ProcessMemorySampler:
    pid: int | None
    interval_seconds: float = 0.05
    peak_bytes: int | None = None

    async def sample_until(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            observed = process_rss_bytes(self.pid)
            if observed is not None:
                self.peak_bytes = max(self.peak_bytes or 0, observed)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                continue
        observed = process_rss_bytes(self.pid)
        if observed is not None:
            self.peak_bytes = max(self.peak_bytes or 0, observed)
