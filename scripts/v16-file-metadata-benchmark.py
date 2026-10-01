"""Explicit synthetic Windows metadata benchmark; no user-file input accepted."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


class Counters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]


def memory() -> tuple[int, int]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)


def source_hashes() -> dict[str, str]:
    paths = ("siyi/app/sandbox.py", "siyi/app/workspace/file_metadata.py", "siyi/app/workspace/file_recovery.py")
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Windows RSS measurement required")
    evidence = ROOT / "build" / "v1600-evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    # Refuse to fill a near-full volume. Fixture is a real logical 1 GiB file;
    # truncate is not an assertion of a cold disk or a physically sparse extent.
    if shutil.disk_usage(evidence).free < 3 * 1024**3:
        parser.error("At least 3 GiB free space is required for the synthetic fixture")
    before = source_hashes()
    with tempfile.TemporaryDirectory(prefix="metadata-benchmark-", dir=evidence) as directory:
        stage = Path(directory)
        os.environ["AGENT_DATA_ROOT"] = str(stage / "runtime")
        os.environ["AGENT_DESKTOP_DATA_DIRECTORY"] = str(stage / "runtime")
        os.environ["AGENT_DATABASE_PATH"] = str(stage / "runtime.db")
        os.environ["AGENT_LOG_PATH"] = str(stage / "runtime.log")
        sys.path.insert(0, str(ROOT / "siyi"))
        from app.database import init_db
        from app.sandbox import execute_tool

        init_db()
        workspace = stage / "workspace"
        workspace.mkdir()
        fixture = workspace / "合成 1 GiB.bin"
        with fixture.open("wb") as stream:
            stream.truncate(1024**3)
        baseline_rss, baseline_peak = memory()
        samples = []
        for number in range(5):
            started = time.perf_counter()
            result = execute_tool(str(workspace), "readonly", "file_metadata", {"path": fixture.name})
            elapsed = (time.perf_counter() - started) * 1000
            rss, peak = memory()
            samples.append({"sample": number + 1, "elapsed_ms": round(elapsed, 2), "rss_bytes": rss,
                            "process_peak_rss_bytes": peak, "rss_above_baseline_bytes": max(0, rss - baseline_rss),
                            "success": result.get("success") is True, "size": result.get("size"),
                            "encoding_probe_bytes": result.get("encoding_probe_bytes"), "sha256": result.get("sha256")})
    after = source_hashes()
    peak_extra = max(0, max(item["process_peak_rss_bytes"] for item in samples) - baseline_rss)
    passed = (before == after and all(item["success"] and item["size"] == 1024**3 for item in samples)
              and len({item["sha256"] for item in samples}) == 1 and peak_extra <= 64 * 1024**2)
    report = {"schema_version": 1, "requirement_id": "V160-F03-METADATA-RSS", "status": "PASS" if passed else "FAIL",
              "actual_run": True, "kind": "synthetic_local_performance", "platform": platform.platform(),
              "python": sys.version, "cache_mode": "OS cache uncontrolled; first sample then four repeated reads; no cache flush",
              "fixture_bytes": 1024**3, "rss_budget_bytes": 64 * 1024**2, "rss_baseline_bytes": baseline_rss,
              "peak_before_measurement_bytes": baseline_peak, "peak_extra_bytes": peak_extra,
              "median_ms": statistics.median(item["elapsed_ms"] for item in samples),
              "worst_ms": max(item["elapsed_ms"] for item in samples), "samples": samples,
              "source_hashes": before, "source_unchanged_during_run": before == after,
              "baseline_comparison": "not_run; measured against explicit RSS budget, not previous-release latency",
              "fixture_retained": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "median_ms", "worst_ms", "peak_extra_bytes", "source_unchanged_during_run")}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
