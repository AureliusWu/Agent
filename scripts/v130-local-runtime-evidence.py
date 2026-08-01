from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95)))]


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_sample(pid: int) -> dict[str, float | int] | None:
    ctypes.windll.kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = ctypes.windll.kernel32.OpenProcess(0x0410, False, pid)
    if not handle:
        return None
    try:
        created = ctypes.c_ulonglong(); exited = ctypes.c_ulonglong(); kernel = ctypes.c_ulonglong(); user = ctypes.c_ulonglong()
        memory = ProcessMemoryCounters(); memory.cb = ctypes.sizeof(memory)
        times_ok = ctypes.windll.kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user))
        memory_ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb)
        return {
            "cpu_ms": round((kernel.value + user.value) / 10_000, 3) if times_ok else 0,
            "rss_bytes": int(memory.WorkingSetSize) if memory_ok else 0,
            "peak_rss_bytes": int(memory.PeakWorkingSetSize) if memory_ok else 0,
        }
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def gpu_compute_processes() -> list[dict[str, int]]:
    result = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    processes = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                processes.append({"pid": int(parts[0]), "used_memory_mib": int(parts[1])})
    return processes


async def main(args: argparse.Namespace) -> dict:
    os.environ["AGENT_DATA_ROOT"] = str(args.output.parent / "isolated-runtime")
    os.environ["AGENT_DATABASE_PATH"] = str(args.output.parent / "isolated-runtime" / "data" / "agent.db")
    from app.database import init_db
    from app.local_runtime.model_manager import model_manager
    from app.local_runtime.ollama_service_manager import ollama_service_manager
    from app.local_runtime.resource_coordinator import resource_coordinator
    from app.tts.manager import TTSManager, create_request
    from app.tts.providers.windows_tts import WindowsTTSProvider

    init_db()
    plan = args.plan.resolve()
    plan_lines = plan.read_text(encoding="utf-8").splitlines()
    service = await ollama_service_manager().status()
    models = await model_manager.list_models()
    target = next((item for item in models if item["name"] == "qwen3:4b"), None)
    model_run = None
    if args.exercise_model and target:
        loaded = await model_manager.preload("qwen3:4b", "5m")
        unloaded = await model_manager.unload("qwen3:4b")
        model_run = {"load": loaded, "unload": unloaded}

    provider = WindowsTTSProvider()
    resources_before_tts = resource_coordinator.snapshot(active_model=None, tts_provider=None)
    voices = await provider.list_voices()
    chinese_voice = next(item["name"] for item in voices if item.get("culture") == "zh-CN")
    sample_text = {
        "first_sentence_30": "你好，我是司忆。这是一段用于验证本地中文首句合成性能的语音。",
        "chinese_100": "司忆正在使用本地系统语音完成中文合成、队列播放、停止和缓存验证。所有文本都经过规范化处理，代码、网址、路径、哈希和敏感凭据不会被机械朗读。用户停止任务时，模型生成、语音合成与播放队列会同步中断。",
    }
    synthesis: dict[str, list[dict]] = {}
    for name, text in sample_text.items():
        samples = []
        for index in range(3):
            output = args.output.parent / f"{name}-{index}.wav"
            started = time.perf_counter()
            result = await provider.synthesize(create_request({"request_id": f"{name}-{index}", "text": text, "voice": chinese_voice, "cache": False}), output)
            samples.append({**result, "wall_ms": round((time.perf_counter() - started) * 1000, 3), "bytes": output.stat().st_size})
            output.unlink(missing_ok=True)
        synthesis[name] = samples

    observed_output = args.output.parent / "resource-observed.wav"
    observed_request = create_request({"request_id": "resource-observed", "text": sample_text["chinese_100"], "voice": chinese_voice, "speed": 0.7, "cache": False})
    observed_task = asyncio.create_task(provider.synthesize(observed_request, observed_output))
    process_samples = []
    observed_pid = None
    gpu_samples: list[dict[str, int]] = []
    while not observed_task.done():
        process = provider._active.get("resource-observed")
        if process is not None:
            observed_pid = process.pid
            sample = process_sample(process.pid)
            if sample:
                process_samples.append(sample)
            if not gpu_samples:
                gpu_samples = gpu_compute_processes()
        await asyncio.sleep(0.01)
    await observed_task
    observed_output.unlink(missing_ok=True)

    manager = TTSManager()
    manager.update_settings({"enabled": True, "provider": "windows", "fallback_provider": "windows", "voice": chinese_voice, "cache_enabled": True})
    cache_text = "这是缓存命中延迟测试。"
    first = await manager.synthesize(create_request({"request_id": "cache-first", "idempotency_key": "cache-first", "text": cache_text, "voice": chinese_voice, "cache": True}))
    cache_ms = []
    for index in range(5):
        started = time.perf_counter()
        hit = await manager.synthesize(create_request({"request_id": f"cache-{index}", "idempotency_key": f"cache-{index}", "text": cache_text, "voice": chinese_voice, "cache": True}))
        cache_ms.append(round((time.perf_counter() - started) * 1000, 3))
        assert hit["cached"] is True
    clear_ms = []
    for index in range(5):
        await manager.speak(create_request({"request_id": f"queue-{index}", "idempotency_key": f"queue-{index}", "task_id": "perf", "text": "停止。", "voice": chinese_voice, "cache": True}))
        started = time.perf_counter()
        await manager.clear_queue(task_id="perf")
        clear_ms.append(round((time.perf_counter() - started) * 1000, 3))

    cancel_output = args.output.parent / "cancel.wav"
    cancel_request = create_request({"request_id": "cancel", "text": "立即停止语音播放。" * 12, "voice": chinese_voice, "speed": 0.5, "cache": False})
    running = asyncio.create_task(provider.synthesize(cancel_request, cancel_output))
    for _ in range(100):
        if provider.get_status()["active_requests"]:
            break
        await asyncio.sleep(0.01)
    cancel_started = time.perf_counter()
    cancelled = await provider.cancel("cancel")
    try:
        await running
    except Exception:
        pass
    stop_ms = round((time.perf_counter() - cancel_started) * 1000, 3)
    cancel_output.unlink(missing_ok=True)
    resources = resource_coordinator.snapshot(active_model=None, tts_provider="windows")
    await manager.shutdown()

    first_ms = [item["wall_ms"] for item in synthesis["first_sentence_30"]]
    gpu_usage_bytes = next((item["used_memory_mib"] * 1024 * 1024 for item in gpu_samples if item["pid"] == observed_pid), 0)
    report = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "plan": {"path": str(plan), "sha256": sha256(plan), "line_count": len(plan_lines)},
        "ollama": {"service": service, "models": models, "qwen3_4b_exercise": model_run},
        "tts": {
            "provider": "windows",
            "voice": chinese_voice,
            "device": "cpu",
            "synthesis": synthesis,
            "first_sentence_median_ms": statistics.median(first_ms),
            "first_sentence_p95_ms": percentile95(first_ms),
            "cache_seed": first,
            "cache_hit_ms": cache_ms,
            "cache_hit_median_ms": statistics.median(cache_ms),
            "queue_clear_ms": clear_ms,
            "queue_clear_median_ms": statistics.median(clear_ms),
            "stop_ms": stop_ms,
            "cancel_requested": cancelled,
            "resources_after": resources,
            "resources_before": resources_before_tts,
            "idle_backend_rss_delta_bytes": (
                int(resources["backend_rss_bytes"]) - int(resources_before_tts["backend_rss_bytes"])
                if resources.get("backend_rss_bytes") is not None and resources_before_tts.get("backend_rss_bytes") is not None else None
            ),
            "synthesis_process": {
                "sample_count": len(process_samples),
                "cpu_ms": max((item["cpu_ms"] for item in process_samples), default=0),
                "peak_rss_bytes": max((item["peak_rss_bytes"] for item in process_samples), default=0),
            },
            "gpu_process_observation": {"tts_pid": observed_pid, "compute_processes": gpu_samples},
            "gpu_usage_bytes": gpu_usage_bytes,
        },
        "thresholds": {
            "first_sentence_le_1500_ms": statistics.median(first_ms) <= 1500,
            "cache_hit_le_200_ms": statistics.median(cache_ms) <= 200,
            "queue_clear_le_100_ms": statistics.median(clear_ms) <= 100,
            "stop_le_200_ms": stop_ms <= 200,
            "tts_gpu_zero": gpu_usage_bytes == 0,
            "tts_idle_memory_le_500mb": (
                abs(int(resources["backend_rss_bytes"]) - int(resources_before_tts["backend_rss_bytes"])) <= 500 * 1024 * 1024
                if resources.get("backend_rss_bytes") is not None and resources_before_tts.get("backend_rss_bytes") is not None else False
            ),
        },
    }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exercise-model", action="store_true")
    options = parser.parse_args()
    options.output.parent.mkdir(parents=True, exist_ok=True)
    payload = asyncio.run(main(options))
    options.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(options.output), "thresholds": payload["thresholds"]}, ensure_ascii=False))
