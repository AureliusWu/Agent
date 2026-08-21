from __future__ import annotations

"""Collect operator-assisted, source-bound v14 A23 endurance evidence.

The collector launches the real candidate desktop executable with a fresh,
test-owned data directory.  The operator performs every requested action in
that desktop window and confirms a freshly generated challenge.  The script
never records audio or transcript text.  It samples only aggregate process,
resource, device, filesystem and database counters, then removes the isolated
runtime after the exact owned process tree has stopped.

This is intentionally not an unattended soak.  A real run requires a TTY and
``--execute`` and has a hard minimum duration of 30 minutes.
"""

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_RUNNER = ROOT / "scripts" / "v14-evidence-runner.py"
TARGET_VERSION = "14.0.0"
REPORT_TYPE = "v14_a23_endurance_live_evidence"
PRODUCER = "scripts/v14-a23-endurance-evidence.py"
MINIMUM_DURATION_SECONDS = 30 * 60
SAMPLE_INTERVAL_SECONDS = 15.0
MAX_CONFIRMATION_SECONDS = 5 * 60
MIN_CONFIRMATION_SECONDS = 0.25
MAX_RSS_GROWTH_BYTES = 512 * 1024 * 1024
MAX_HANDLE_GROWTH = 96
MAX_CACHE_GROWTH_BYTES = 256 * 1024 * 1024
MAX_TEMP_GROWTH_BYTES = 64 * 1024 * 1024
MAX_FINAL_GPU_DELTA_MIB = 256
ACTION_REQUIREMENTS: dict[str, int] = {
    "recording_start_stop": 100,
    "stt": 50,
    "tts": 50,
    "cancel": 20,
    "tts_interrupt": 20,
    "device_change": 10,
    "model_load_unload": 10,
    "agent_stop": 10,
    "cache_roundtrip": 5,
}
ACTION_LABELS = {
    "recording_start_stop": "在候选桌面开始一次真实录音并停止，确认录音链路已回到可继续状态",
    "stt": "完成一次真实麦克风录音并等待本地 STT 返回可见转写",
    "tts": "让候选桌面合成并实际播放固定公开测试句一次",
    "cancel": "开始录音或转写后执行取消，并确认状态已收敛",
    "tts_interrupt": "开始 TTS 播放后执行打断，并确认声音停止",
    "device_change": "切换真实麦克风，或触发一次可审计的模拟 devicechange 后恢复可用设备",
    "model_load_unload": "在本地模型面板完成一次模型加载并卸载，确认资源释放",
    "agent_stop": "提交一个 Agent 任务，在运行或排队时执行全局停止并确认任务已取消",
    "cache_roundtrip": "连续两次播放同一固定公开测试句，确认第二次缓存读取仍可播放",
}
REQUIRED_CHECKS = (
    "explicit_execute_and_interactive_operator",
    "fresh_test_owned_runtime",
    "candidate_release_identity",
    "desktop_and_sidecar_pid_ownership",
    "continuous_api_health",
    "minimum_30_minute_duration",
    "operator_challenges_valid",
    "required_action_counts",
    "database_action_corroboration",
    "ram_vram_handle_process_trends_collected",
    "audio_device_trend_collected_and_restored",
    "temporary_and_cache_growth_bounded",
    "zero_failure_rate",
    "owned_processes_released",
    "test_owned_runtime_removed",
    "external_processes_protected",
)
SPAWN_RE = re.compile(r"desktop sidecar spawned pid=(\d+) port=(\d+)")


class EnduranceEvidenceError(RuntimeError):
    """The real A23 run could not satisfy its fail-closed contract."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_identity() -> dict[str, Any]:
    supplied = os.environ.get("SIYI_V14_EVIDENCE_SOURCE_IDENTITY")
    if supplied:
        try:
            identity = json.loads(supplied)
        except json.JSONDecodeError as exc:
            raise EnduranceEvidenceError("runner supplied invalid source identity") from exc
    else:
        spec = importlib.util.spec_from_file_location("v14_a23_source_identity", EVIDENCE_RUNNER)
        if spec is None or spec.loader is None:
            raise EnduranceEvidenceError("could not load v14 evidence source identity")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        identity = module.source_identity(ROOT)
    required = ("source_version", "source_commit", "source_tree_fingerprint", "workspace_clean")
    if not isinstance(identity, dict) or any(field not in identity for field in required):
        raise EnduranceEvidenceError("source identity is incomplete")
    if not isinstance(identity["workspace_clean"], bool):
        raise EnduranceEvidenceError("source identity workspace_clean must be boolean")
    return {field: identity[field] for field in required}


def resolve_output_path(value: str) -> Path:
    evidence_root = (ROOT / "build" / "v1400-evidence").resolve()
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (ROOT / candidate).resolve()
    try:
        resolved.relative_to(evidence_root)
    except ValueError as exc:
        raise EnduranceEvidenceError("--output must stay under build/v1400-evidence") from exc
    if resolved.suffix.casefold() != ".json":
        raise EnduranceEvidenceError("--output must end in .json")
    return resolved


def resolve_candidate(value: str) -> Path:
    candidate = Path(value).expanduser().resolve(strict=True)
    if not candidate.is_file() or candidate.suffix.casefold() != ".exe":
        raise EnduranceEvidenceError("--candidate-exe must be an existing Windows executable")
    expected_name = "司忆.exe"
    config = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
    try:
        expected_name = f"{json.loads(config.read_text(encoding='utf-8'))['mainBinaryName']}.exe"
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        pass
    if candidate.name.casefold() != expected_name.casefold():
        raise EnduranceEvidenceError(f"candidate must be the packaged desktop executable {expected_name}")
    return candidate


def write_json_immutable(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileExistsError as exc:
        raise EnduranceEvidenceError(f"refusing to overwrite evidence: {path.name}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _powershell_json(script: str, *, timeout: float = 15.0) -> Any:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise EnduranceEvidenceError("Windows resource query failed")
    text = completed.stdout.strip()
    return json.loads(text) if text else []


def _process_tree_rows(root_pid: int) -> list[dict[str, Any]]:
    script = (
        "$all=@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,"
        "ExecutablePath,CreationDate);$ids=[System.Collections.Generic.HashSet[int]]::new();"
        f"[void]$ids.Add({root_pid});do{{$changed=$false;foreach($item in $all){{if($ids.Contains("
        "[int]$item.ParentProcessId)-and -not $ids.Contains([int]$item.ProcessId)){[void]$ids.Add("
        "[int]$item.ProcessId);$changed=$true}}}while($changed);$rows=@();foreach($item in $all){"
        "if($ids.Contains([int]$item.ProcessId)){$p=Get-Process -Id $item.ProcessId -ErrorAction "
        "SilentlyContinue;if($p){$rows+=[pscustomobject]@{pid=[int]$item.ProcessId;ppid=[int]$item."
        "ParentProcessId;name=[string]$item.Name;path=[string]$item.ExecutablePath;created=[string]"
        "$item.CreationDate;rss=[int64]$p.WorkingSet64;private=[int64]$p.PrivateMemorySize64;"
        "handles=[int]$p.HandleCount}}}};$rows|ConvertTo-Json -Compress"
    )
    payload = _powershell_json(script)
    if isinstance(payload, dict):
        payload = [payload]
    return [row for row in payload if isinstance(row, dict)]


def _matching_processes(executable: Path) -> list[dict[str, Any]]:
    escaped = executable.name.replace("'", "''")
    script = (
        f"@(Get-CimInstance Win32_Process -Filter \"Name='{escaped}'\" | Select-Object "
        "ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate)|ConvertTo-Json -Compress"
    )
    payload = _powershell_json(script)
    if isinstance(payload, dict):
        payload = [payload]
    resolved = str(executable.resolve()).casefold()
    return [row for row in payload if str(row.get("ExecutablePath") or "").casefold() == resolved]


def named_process_identities(name: str) -> list[dict[str, Any]]:
    """Fingerprint external processes without retaining command lines or paths."""

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise EnduranceEvidenceError("unsafe process name")
    script = (
        f"@(Get-CimInstance Win32_Process -Filter \"Name='{name}'\" | Select-Object "
        "ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate)|ConvertTo-Json -Compress"
    )
    payload = _powershell_json(script)
    if isinstance(payload, dict):
        payload = [payload]
    identities = []
    for row in payload:
        executable = Path(str(row.get("ExecutablePath") or ""))
        identities.append(
            {
                "pid": int(row.get("ProcessId") or 0),
                "ppid": int(row.get("ParentProcessId") or 0),
                "name": str(row.get("Name") or ""),
                "creation_time": str(row.get("CreationDate") or ""),
                "executable_sha256": sha256_file(executable) if executable.is_file() else "UNAVAILABLE",
            }
        )
    return sorted(identities, key=lambda item: (item["pid"], item["creation_time"]))


def _safe_process_row(row: dict[str, Any]) -> dict[str, Any]:
    executable = Path(str(row.get("path") or ""))
    digest = sha256_file(executable) if executable.is_file() else "UNAVAILABLE"
    return {
        "pid": int(row.get("pid") or 0),
        "ppid": int(row.get("ppid") or 0),
        "name": str(row.get("name") or ""),
        "creation_time": str(row.get("created") or ""),
        "executable_sha256": digest,
        "rss_bytes": int(row.get("rss") or 0),
        "private_bytes": int(row.get("private") or 0),
        "handle_count": int(row.get("handles") or 0),
    }


def process_snapshot(desktop_pid: int) -> dict[str, Any]:
    safe_rows = [_safe_process_row(row) for row in _process_tree_rows(desktop_pid)]
    return {
        "processes": safe_rows,
        "process_count": len(safe_rows),
        "rss_bytes": sum(row["rss_bytes"] for row in safe_rows),
        "private_bytes": sum(row["private_bytes"] for row in safe_rows),
        "handle_count": sum(row["handle_count"] for row in safe_rows),
    }


def system_memory_snapshot() -> dict[str, int]:
    payload = _powershell_json(
        "$os=Get-CimInstance Win32_OperatingSystem;[pscustomobject]@{total=[int64]$os.TotalVisibleMemorySize*1024;free=[int64]$os.FreePhysicalMemory*1024}|ConvertTo-Json -Compress"
    )
    if not isinstance(payload, dict):
        raise EnduranceEvidenceError("system memory query returned invalid data")
    total = int(payload.get("total") or 0)
    free = int(payload.get("free") or 0)
    if total <= 0 or free < 0 or free > total:
        raise EnduranceEvidenceError("system memory query returned invalid values")
    return {"total_bytes": total, "free_bytes": free, "used_bytes": total - free}


def audio_device_snapshot() -> dict[str, Any]:
    script = (
        "$items=@(Get-PnpDevice -Class AudioEndpoint -PresentOnly -ErrorAction Stop | "
        "Select-Object Status,FriendlyName,InstanceId);$items|ConvertTo-Json -Compress"
    )
    payload = _powershell_json(script)
    if isinstance(payload, dict):
        payload = [payload]
    devices = []
    for item in payload:
        stable = f"{item.get('FriendlyName','')}\0{item.get('InstanceId','')}"
        devices.append(
            {
                "identity_sha256": sha256_bytes(stable.encode("utf-8", errors="replace")),
                "status": str(item.get("Status") or "UNKNOWN"),
            }
        )
    devices.sort(key=lambda item: item["identity_sha256"])
    return {"available": True, "count": len(devices), "devices": devices}


def gpu_snapshot(owned_pids: set[int]) -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        gpu = subprocess.run(command, capture_output=True, check=False, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "gpu_count": 0, "used_mib": None, "owned_used_mib": None}
    if gpu.returncode != 0:
        return {"available": False, "gpu_count": 0, "used_mib": None, "owned_used_mib": None}
    rows = [row.strip() for row in gpu.stdout.splitlines() if row.strip()]
    used = sum(int(row.split(",")[0].strip()) for row in rows)
    compute = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    owned = 0
    if compute.returncode == 0:
        for row in compute.stdout.splitlines():
            fields = [field.strip() for field in row.split(",")]
            if len(fields) >= 2 and fields[0].isdigit() and int(fields[0]) in owned_pids:
                owned += int(fields[1])
    return {"available": True, "gpu_count": len(rows), "used_mib": used, "owned_used_mib": owned}


def directory_metrics(path: Path) -> dict[str, int]:
    files = 0
    size = 0
    links = 0
    if path.exists():
        for candidate in path.rglob("*"):
            if candidate.is_symlink():
                links += 1
            elif candidate.is_file():
                files += 1
                size += candidate.stat().st_size
    return {"file_count": files, "bytes": size, "link_count": links}


def filesystem_snapshot(runtime: Path) -> dict[str, Any]:
    temp = directory_metrics(runtime / "temp")
    cache = directory_metrics(runtime / "cache")
    audio_files = 0
    for extension in ("*.wav", "*.mp3", "*.ogg", "*.webm"):
        audio_files += sum(1 for item in runtime.rglob(extension) if item.is_file())
    return {"temp": temp, "cache": cache, "audio_file_count": audio_files}


def api_health(port: int, *, timeout: float = 2.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise EnduranceEvidenceError("candidate sidecar health API is unavailable") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise EnduranceEvidenceError("candidate sidecar health API is unhealthy")
    return payload


def find_sidecar(runtime: Path, desktop_pid: int, process: subprocess.Popen[bytes]) -> tuple[int, int]:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise EnduranceEvidenceError("candidate desktop exited before Sidecar became ready")
        logs = sorted((runtime / "logs").glob("siyi-shell*.log")) if (runtime / "logs").is_dir() else []
        for log in reversed(logs):
            try:
                matches = SPAWN_RE.findall(log.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if matches:
                sidecar_pid, port = (int(value) for value in matches[-1])
                rows = {int(row.get("pid") or 0): row for row in _process_tree_rows(desktop_pid)}
                child = rows.get(sidecar_pid)
                if child and int(child.get("ppid") or 0) == desktop_pid:
                    api_health(port)
                    return sidecar_pid, port
        time.sleep(0.25)
    raise EnduranceEvidenceError("candidate Sidecar PID/port was not discovered within 45 seconds")


def validate_candidate_health(health: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    build = health.get("build")
    if not isinstance(build, dict):
        raise EnduranceEvidenceError("candidate health is missing build identity")
    expected_state = "CLEAN" if source["workspace_clean"] else "DIRTY"
    checks = {
        "runtime_version": health.get("version") == TARGET_VERSION,
        "product_version": build.get("product_version") == TARGET_VERSION,
        "embedded": build.get("embedded") is True,
        "build_type": build.get("build_type") == "Release",
        "source_commit": build.get("git_commit") == source["source_commit"],
        "source_fingerprint": build.get("source_fingerprint") == source["source_tree_fingerprint"],
        "workspace_state": build.get("workspace_state") == expected_state,
        "component": build.get("component") == "sidecar",
    }
    if not all(checks.values()):
        raise EnduranceEvidenceError("candidate package identity does not match the current v14 source")
    return {
        "version": health["version"],
        "build_id": build.get("build_id"),
        "component_build_id": build.get("component_build_id"),
        "git_commit": build.get("git_commit"),
        "source_fingerprint": build.get("source_fingerprint"),
        "workspace_state": build.get("workspace_state"),
        "embedded": build.get("embedded"),
        "checks": checks,
    }


def database_snapshot(database: Path) -> dict[str, Any]:
    uri = f"file:{database.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        def scalar(query: str) -> int:
            return int(db.execute(query).fetchone()[0])

        def grouped(query: str) -> dict[str, int]:
            return {str(key): int(value) for key, value in db.execute(query).fetchall()}

        return {
            "voice_sessions": scalar("SELECT COUNT(*) FROM voice_sessions"),
            "voice_states": grouped("SELECT state,COUNT(*) FROM voice_sessions GROUP BY state"),
            "voice_events": grouped("SELECT event,COUNT(*) FROM voice_event_records GROUP BY event"),
            "stt_requests": scalar("SELECT COUNT(*) FROM stt_requests"),
            "stt_statuses": grouped("SELECT status,COUNT(*) FROM stt_requests GROUP BY status"),
            "tts_requests": scalar("SELECT COUNT(*) FROM tts_requests"),
            "tts_statuses": grouped("SELECT status,COUNT(*) FROM tts_requests GROUP BY status"),
            "tts_cached_requests": scalar("SELECT COUNT(cache_id) FROM tts_requests"),
            "tts_distinct_cache_ids": scalar("SELECT COUNT(DISTINCT cache_id) FROM tts_requests"),
            "model_load_actions": grouped("SELECT action,COUNT(*) FROM model_load_records GROUP BY action"),
            "task_statuses": grouped("SELECT status,COUNT(*) FROM agent_tasks GROUP BY status"),
        }


def _mapping_delta(after: dict[str, int], before: dict[str, int], key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


def database_corroboration(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    voice_events_before = before["voice_events"]
    voice_events_after = after["voice_events"]
    checks = {
        "recording_started": _mapping_delta(voice_events_after, voice_events_before, "RECORDING_STARTED") >= 100,
        "recording_stopped": _mapping_delta(voice_events_after, voice_events_before, "RECORDING_STOPPED") >= 100,
        "stt_completed": after["stt_requests"] - before["stt_requests"] >= 50,
        "tts_created": after["tts_requests"] - before["tts_requests"] >= 50,
        "cancelled": _mapping_delta(after["voice_states"], before["voice_states"], "CANCELLED") >= 20,
        "tts_interrupted": _mapping_delta(voice_events_after, voice_events_before, "TTS_STOPPED") >= 20,
        "model_loaded": _mapping_delta(after["model_load_actions"], before["model_load_actions"], "load") >= 10,
        "model_unloaded": _mapping_delta(after["model_load_actions"], before["model_load_actions"], "unload") >= 10,
        "agent_cancelled": _mapping_delta(after["task_statuses"], before["task_statuses"], "cancelled") >= 10,
        "cache_reused": (
            after["tts_cached_requests"] - before["tts_cached_requests"]
            > after["tts_distinct_cache_ids"] - before["tts_distinct_cache_ids"]
        ),
    }
    deltas = {
        "voice_sessions": after["voice_sessions"] - before["voice_sessions"],
        "stt_requests": after["stt_requests"] - before["stt_requests"],
        "tts_requests": after["tts_requests"] - before["tts_requests"],
        "voice_events": {
            key: _mapping_delta(voice_events_after, voice_events_before, key)
            for key in ("RECORDING_STARTED", "RECORDING_STOPPED", "TTS_STOPPED")
        },
        "voice_cancelled": _mapping_delta(after["voice_states"], before["voice_states"], "CANCELLED"),
        "model_load_actions": {
            key: _mapping_delta(after["model_load_actions"], before["model_load_actions"], key)
            for key in ("load", "unload")
        },
        "agent_cancelled": _mapping_delta(after["task_statuses"], before["task_statuses"], "cancelled"),
        "tts_cached_requests": after["tts_cached_requests"] - before["tts_cached_requests"],
        "tts_distinct_cache_ids": after["tts_distinct_cache_ids"] - before["tts_distinct_cache_ids"],
    }
    return {"checks": checks, "deltas": deltas, "passed": all(checks.values())}


def build_action_schedule(requirements: dict[str, int] | None = None) -> list[str]:
    remaining = dict(requirements or ACTION_REQUIREMENTS)
    schedule: list[str] = []
    while any(value > 0 for value in remaining.values()):
        for action in remaining:
            if remaining[action] > 0:
                schedule.append(action)
                remaining[action] -= 1
    return schedule


def confirmation_digest(challenge: str, outcome: str, variant: str = "") -> str:
    value = " ".join(item for item in (outcome, challenge, variant) if item)
    return sha256_bytes(value.encode("ascii"))


def console_write(value: str) -> None:
    """Write prompts to the real Windows console, bypassing runner capture."""

    if os.name != "nt":
        sys.stdout.write(value)
        sys.stdout.flush()
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        "CONOUT$", 0x40000000, 0x00000003, None, 3, 0, None
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise EnduranceEvidenceError("A23 requires a visible Windows console")
    written = ctypes.c_ulong(0)
    try:
        if not kernel32.WriteConsoleW(
            ctypes.c_void_p(handle), value, len(value), ctypes.byref(written), None
        ):
            raise EnduranceEvidenceError("could not write the A23 operator challenge")
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def console_input(prompt: str) -> str:
    console_write(prompt)
    value = sys.stdin.readline()
    if value == "":
        raise EnduranceEvidenceError("A23 operator console input closed")
    return value.rstrip("\r\n")


def confirm_action(
    action: str,
    ordinal: int,
    total: int,
    *,
    input_fn: Callable[[str], str] = console_input,
    monotonic_fn: Callable[[], float] = time.monotonic,
    now_fn: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    challenge = secrets.token_hex(12).upper()
    issued_at = now_fn()
    started = monotonic_fn()
    variants = "；设备变化追加 REAL 或 SIMULATED，模型循环追加 OLLAMA 或 STT" if action in {"device_change", "model_load_unload"} else ""
    prompt = (
        f"\n[A23 {ordinal}/{total}] {ACTION_LABELS[action]}\n"
        f"完成后输入 PASS {challenge}{variants}；失败输入 FAIL {challenge} CODE："
    )
    raw = input_fn(prompt).strip()
    elapsed = monotonic_fn() - started
    completed_at = now_fn()
    fields = raw.split()
    outcome = fields[0].upper() if fields else "INVALID"
    supplied = fields[1].upper() if len(fields) > 1 else ""
    variant = fields[2].upper() if len(fields) > 2 else ""
    allowed_variant = (
        action == "device_change" and variant in {"REAL", "SIMULATED"}
    ) or (action == "model_load_unload" and variant in {"OLLAMA", "STT"})
    exact_length = len(fields) == (3 if action in {"device_change", "model_load_unload"} else 2)
    valid = (
        outcome in {"PASS", "FAIL"}
        and supplied == challenge
        and exact_length
        and (allowed_variant or action not in {"device_change", "model_load_unload"})
        and MIN_CONFIRMATION_SECONDS <= elapsed <= MAX_CONFIRMATION_SECONDS
    )
    return {
        "sequence": ordinal,
        "action": action,
        "challenge": challenge,
        "issued_at": issued_at,
        "completed_at": completed_at,
        "elapsed_seconds": round(elapsed, 3),
        "outcome": outcome if valid else "INVALID",
        "variant": variant or None,
        "confirmation_sha256": confirmation_digest(challenge, outcome, variant) if valid else None,
        "valid": valid,
    }


def resource_snapshot(runtime: Path, desktop_pid: int, sidecar_pid: int, port: int) -> dict[str, Any]:
    process = process_snapshot(desktop_pid)
    pids = {int(row["pid"]) for row in process["processes"]}
    health = api_health(port)
    return {
        "recorded_at": utc_now(),
        "monotonic_seconds": round(time.monotonic(), 3),
        "desktop_alive": desktop_pid in pids,
        "sidecar_alive": sidecar_pid in pids,
        "api_healthy": health.get("status") == "ok",
        "system_memory": system_memory_snapshot(),
        "process": process,
        "gpu": gpu_snapshot(pids),
        "audio_devices": audio_device_snapshot(),
        "filesystem": filesystem_snapshot(runtime),
    }


def _sampler_loop(
    stop: threading.Event,
    samples: list[dict[str, Any]],
    errors: list[str],
    runtime: Path,
    desktop_pid: int,
    sidecar_pid: int,
    port: int,
) -> None:
    while not stop.is_set():
        try:
            samples.append(resource_snapshot(runtime, desktop_pid, sidecar_pid, port))
        except Exception as exc:
            errors.append(type(exc).__name__)
        stop.wait(SAMPLE_INTERVAL_SECONDS)


def _same_owned_process(expected: dict[str, Any]) -> bool:
    pid = int(expected["pid"])
    rows = [_safe_process_row(row) for row in _process_tree_rows(pid)]
    current = next((row for row in rows if row["pid"] == pid), None)
    return bool(
        current
        and current["creation_time"] == expected["creation_time"]
        and current["executable_sha256"] == expected["executable_sha256"]
    )


def stop_owned_desktop(process: subprocess.Popen[bytes], identity: dict[str, Any]) -> dict[str, Any]:
    result = {"close_requested": False, "forced": False, "desktop_stopped": False}
    if process.poll() is not None:
        result["desktop_stopped"] = True
        return result
    if not _same_owned_process(identity):
        result["ownership_lost"] = True
        return result
    close = _powershell_json(
        f"$p=Get-Process -Id {process.pid} -ErrorAction Stop;[pscustomobject]@{{closed=$p.CloseMainWindow()}}|ConvertTo-Json -Compress"
    )
    result["close_requested"] = bool(close.get("closed")) if isinstance(close, dict) else False
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if _same_owned_process(identity):
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            result["forced"] = True
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    result["desktop_stopped"] = process.poll() is not None
    return result


def cleanup_runtime(runtime: Path) -> dict[str, Any]:
    evidence_root = (ROOT / "build" / "v1400-evidence").resolve()
    resolved = runtime.resolve()
    try:
        relative = resolved.relative_to(evidence_root)
    except ValueError:
        return {"removed": False, "reason": "runtime escaped evidence root"}
    if not relative.as_posix().startswith("a23-runtime-"):
        return {"removed": False, "reason": "runtime name was not collector-owned"}
    if any(
        item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction())
        for item in resolved.rglob("*")
    ):
        return {"removed": False, "reason": "runtime contains a link and was retained for review"}
    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        return {"removed": False, "reason": f"runtime cleanup failed: {type(exc).__name__}"}
    return {"removed": not resolved.exists(), "reason": None}


def _counts_from_events(events: list[dict[str, Any]]) -> dict[str, int]:
    return dict(
        Counter(
            str(event.get("action"))
            for event in events
            if event_is_valid(event) and event.get("outcome") == "PASS"
        )
    )


def event_is_valid(event: dict[str, Any]) -> bool:
    challenge = event.get("challenge")
    action = event.get("action")
    outcome = event.get("outcome")
    variant = event.get("variant") or ""
    if (
        event.get("valid") is not True
        or action not in ACTION_REQUIREMENTS
        or outcome not in {"PASS", "FAIL"}
        or not isinstance(challenge, str)
        or not re.fullmatch(r"[0-9A-F]{24}", challenge)
    ):
        return False
    if action == "device_change" and variant not in {"REAL", "SIMULATED"}:
        return False
    if action == "model_load_unload" and variant not in {"OLLAMA", "STT"}:
        return False
    if action not in {"device_change", "model_load_unload"} and variant:
        return False
    elapsed = event.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or not MIN_CONFIRMATION_SECONDS <= elapsed <= MAX_CONFIRMATION_SECONDS:
        return False
    try:
        wall_elapsed = (parse_utc(str(event["completed_at"])) - parse_utc(str(event["issued_at"]))).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    if wall_elapsed < 0 or abs(wall_elapsed - float(elapsed)) > 10:
        return False
    return event.get("confirmation_sha256") == confirmation_digest(challenge, outcome, variant)


def _resources_assessment(samples: list[dict[str, Any]], cleanup: dict[str, Any]) -> dict[str, Any]:
    first = samples[0]
    last = samples[-1]
    rss_growth = int(last["process"]["rss_bytes"]) - int(first["process"]["rss_bytes"])
    handle_growth = int(last["process"]["handle_count"]) - int(first["process"]["handle_count"])
    cache_growth = int(last["filesystem"]["cache"]["bytes"]) - int(first["filesystem"]["cache"]["bytes"])
    temp_growth = int(last["filesystem"]["temp"]["bytes"]) - int(first["filesystem"]["temp"]["bytes"])
    gpu_available = all(sample["gpu"].get("available") is True for sample in samples)
    devices_available = all(sample["audio_devices"].get("available") is True for sample in samples)
    devices_restored = first["audio_devices"] == last["audio_devices"]
    checks = {
        "rss_growth_bounded": rss_growth <= MAX_RSS_GROWTH_BYTES,
        "handle_growth_bounded": handle_growth <= MAX_HANDLE_GROWTH,
        "cache_growth_bounded": cache_growth <= MAX_CACHE_GROWTH_BYTES,
        "temp_growth_bounded": temp_growth <= MAX_TEMP_GROWTH_BYTES,
        "gpu_collected": gpu_available,
        "audio_devices_collected": devices_available,
        "audio_devices_restored": devices_restored,
        "no_sample_liveness_failure": all(
            sample["desktop_alive"] and sample["sidecar_alive"] and sample["api_healthy"] for sample in samples
        ),
        "owned_processes_released": cleanup.get("owned_processes_released") is True,
        "owned_gpu_released": cleanup.get("owned_gpu_mib_after", 1) == 0,
        "global_gpu_returned": cleanup.get("global_gpu_delta_mib", MAX_FINAL_GPU_DELTA_MIB + 1) <= MAX_FINAL_GPU_DELTA_MIB,
    }
    return {
        "checks": checks,
        "rss_growth_bytes": rss_growth,
        "handle_growth": handle_growth,
        "cache_growth_bytes": cache_growth,
        "temp_growth_bytes": temp_growth,
        "sample_count": len(samples),
        "passed": all(checks.values()),
    }


def evaluate_report(report: dict[str, Any]) -> dict[str, Any]:
    events = report.get("events") if isinstance(report.get("events"), list) else []
    samples = report.get("resource_samples") if isinstance(report.get("resource_samples"), list) else []
    counts = _counts_from_events(events)
    valid_challenges = bool(events) and all(event_is_valid(event) for event in events)
    valid_challenges = valid_challenges and len({event.get("challenge") for event in events}) == len(events)
    valid_challenges = valid_challenges and [event.get("sequence") for event in events] == list(
        range(1, len(events) + 1)
    )
    duration = float(report.get("duration_seconds") or 0)
    database = report.get("database_corroboration")
    cleanup = report.get("cleanup") if isinstance(report.get("cleanup"), dict) else {}
    resources = _resources_assessment(samples, cleanup) if len(samples) >= 2 else {"passed": False, "checks": {}}
    checks = {
        "explicit_execute_and_interactive_operator": report.get("interactive_operator") is True,
        "fresh_test_owned_runtime": report.get("test_owned_runtime") is True,
        "candidate_release_identity": report.get("candidate", {}).get("identity_verified") is True,
        "desktop_and_sidecar_pid_ownership": report.get("ownership", {}).get("verified") is True,
        "continuous_api_health": not report.get("sampler_errors") and resources.get("checks", {}).get("no_sample_liveness_failure") is True,
        "minimum_30_minute_duration": duration >= MINIMUM_DURATION_SECONDS,
        "operator_challenges_valid": valid_challenges,
        "required_action_counts": all(counts.get(action, 0) >= minimum for action, minimum in ACTION_REQUIREMENTS.items()),
        "database_action_corroboration": isinstance(database, dict) and database.get("passed") is True,
        "ram_vram_handle_process_trends_collected": len(samples) >= 2 and resources.get("passed") is True,
        "audio_device_trend_collected_and_restored": resources.get("checks", {}).get("audio_devices_restored") is True,
        "temporary_and_cache_growth_bounded": all(
            resources.get("checks", {}).get(key) is True
            for key in ("cache_growth_bounded", "temp_growth_bounded")
        ),
        "zero_failure_rate": all(event.get("outcome") == "PASS" for event in events),
        "owned_processes_released": cleanup.get("owned_processes_released") is True and cleanup.get("forced") is not True,
        "test_owned_runtime_removed": cleanup.get("runtime_removed") is True,
        "external_processes_protected": cleanup.get("external_processes_protected") is True,
    }
    return {
        "checks": {name: {"passed": bool(checks.get(name))} for name in REQUIRED_CHECKS},
        "counts": counts,
        "resources": resources,
        "status": "PASS" if all(checks.values()) else "FAIL",
    }


def _new_report(source: dict[str, Any], candidate: Path, runtime: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "report_type": REPORT_TYPE,
        "producer": PRODUCER,
        "target_version": TARGET_VERSION,
        "recorded_at": utc_now(),
        "actual_run": True,
        "status": "FAIL",
        "source": source,
        "interactive_operator": bool(sys.stdin.isatty()),
        "test_owned_runtime": True,
        "candidate": {
            "filename": candidate.name,
            "sha256": sha256_file(candidate),
            "identity_verified": False,
        },
        "runtime": {"name": runtime.name, "isolated": True, "retained": False},
        "ownership": {"verified": False},
        "events": [],
        "resource_samples": [],
        "sampler_errors": [],
        "limitations": [
            "Operator challenges attest UI actions but do not contain recording or transcript content.",
            "The collector retains aggregate counters only and removes the isolated private runtime.",
        ],
    }


def _launch_candidate(candidate: Path, runtime: Path) -> subprocess.Popen[bytes]:
    if _matching_processes(candidate):
        raise EnduranceEvidenceError("the same candidate executable is already running; close it before A23")
    environment = os.environ.copy()
    environment["AGENT_DESKTOP_DATA_DIRECTORY"] = str(runtime)
    return subprocess.Popen(
        [str(candidate)],
        cwd=candidate.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _identity_for_pid(pid: int) -> dict[str, Any]:
    rows = [_safe_process_row(row) for row in _process_tree_rows(pid)]
    identity = next((row for row in rows if row["pid"] == pid), None)
    if identity is None:
        raise EnduranceEvidenceError("could not bind launched desktop PID identity")
    return identity


def _wait_for_database(database: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if database.is_file():
            try:
                database_snapshot(database)
                return
            except (OSError, sqlite3.Error):
                pass
        time.sleep(0.25)
    raise EnduranceEvidenceError("isolated candidate database did not become ready")


def _prepare_owned_runtime(
    report: dict[str, Any],
    candidate: Path,
    runtime: Path,
    process: subprocess.Popen[bytes],
    source: dict[str, Any],
) -> tuple[dict[str, Any], int, int, Path, dict[str, Any], dict[str, Any]]:
    desktop_identity = _identity_for_pid(process.pid)
    sidecar_pid, port = find_sidecar(runtime, process.pid, process)
    health_identity = validate_candidate_health(api_health(port), source)
    report["candidate"].update({"identity_verified": True, "health": health_identity})
    report["ownership"] = {
        "verified": True,
        "desktop": desktop_identity,
        "sidecar_pid": sidecar_pid,
        "sidecar_parent_pid": process.pid,
        "api_port": port,
        "ownership_policy": "only exact collector-launched PID identities may be stopped",
    }
    database = runtime / "data" / "agent.db"
    _wait_for_database(database)
    before_database = database_snapshot(database)
    first_sample = resource_snapshot(runtime, process.pid, sidecar_pid, port)
    report["resource_samples"].append(first_sample)
    return desktop_identity, sidecar_pid, port, database, before_database, first_sample


def _run_operator_actions(report: dict[str, Any], started: float) -> None:
    schedule = build_action_schedule()
    total = len(schedule)
    for ordinal, action in enumerate(schedule, 1):
        event = confirm_action(action, ordinal, total)
        event["run_elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["events"].append(event)
        if not event["valid"] or event["outcome"] != "PASS":
            raise EnduranceEvidenceError("invalid, failed, stale, or forged operator confirmation")
    while time.monotonic() - started < MINIMUM_DURATION_SECONDS:
        remaining = max(0, MINIMUM_DURATION_SECONDS - (time.monotonic() - started))
        console_write(
            f"\nA23 操作计数已完成，继续在候选桌面正常使用；距 30 分钟下限约 {remaining:.0f} 秒。\n"
        )
        time.sleep(min(30.0, remaining))


def _cleanup_observation(
    process: subprocess.Popen[bytes],
    desktop_identity: dict[str, Any],
    seen_processes: dict[int, dict[str, Any]],
    runtime: Path,
    baseline_gpu: dict[str, Any],
    external_ollama_before: list[dict[str, Any]],
) -> dict[str, Any]:
    stop = stop_owned_desktop(process, desktop_identity)
    time.sleep(1.0)
    alive = []
    for pid, expected in sorted(seen_processes.items()):
        try:
            rows = [_safe_process_row(row) for row in _process_tree_rows(pid)]
        except EnduranceEvidenceError:
            rows = []
        current = next((row for row in rows if row["pid"] == pid), None)
        if current and (
            current["creation_time"] == expected["creation_time"]
            and current["executable_sha256"] == expected["executable_sha256"]
        ):
            alive.append(pid)
    final_gpu = gpu_snapshot(set(seen_processes))
    try:
        external_ollama_after = named_process_identities("ollama.exe")
    except EnduranceEvidenceError:
        external_ollama_after = []
    processes_released = not alive and stop.get("desktop_stopped") is True
    removal = (
        cleanup_runtime(runtime)
        if processes_released
        else {"removed": False, "reason": "owned process remained; runtime retained for safe review"}
    )
    baseline_used = baseline_gpu.get("used_mib")
    final_used = final_gpu.get("used_mib")
    gpu_delta = (
        max(0, int(final_used) - int(baseline_used))
        if baseline_used is not None and final_used is not None
        else MAX_FINAL_GPU_DELTA_MIB + 1
    )
    return {
        **stop,
        "seen_owned_pids": sorted(seen_processes),
        "remaining_owned_pids": alive,
        "owned_processes_released": processes_released,
        "owned_gpu_mib_after": final_gpu.get("owned_used_mib"),
        "global_gpu_delta_mib": gpu_delta,
        "runtime_removed": removal["removed"],
        "runtime_cleanup_reason": removal["reason"],
        "external_ollama_before": external_ollama_before,
        "external_ollama_after": external_ollama_after,
        "external_processes_protected": external_ollama_after == external_ollama_before,
    }


def run_live_evidence(candidate: Path, output: Path) -> dict[str, Any]:
    source = source_identity()
    runtime = ROOT / "build" / "v1400-evidence" / f"a23-runtime-{secrets.token_hex(8)}"
    report = _new_report(source, candidate, runtime)
    process: subprocess.Popen[bytes] | None = None
    desktop_identity: dict[str, Any] | None = None
    sampler_stop = threading.Event()
    sampler: threading.Thread | None = None
    seen_processes: dict[int, dict[str, Any]] = {}
    baseline_gpu = gpu_snapshot(set())
    external_ollama_before: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        if os.name != "nt" or sys.platform != "win32":
            raise EnduranceEvidenceError("A23 formal package endurance requires Windows")
        if not sys.stdin.isatty():
            raise EnduranceEvidenceError("A23 requires an interactive operator TTY")
        if source["source_version"] != TARGET_VERSION:
            raise EnduranceEvidenceError("A23 requires all version sources to be v14.0.0")
        external_ollama_before = named_process_identities("ollama.exe")
        runtime.mkdir(parents=True, exist_ok=False)
        process = _launch_candidate(candidate, runtime)
        (
            desktop_identity,
            sidecar_pid,
            port,
            database,
            before_database,
            first_sample,
        ) = _prepare_owned_runtime(report, candidate, runtime, process, source)
        seen_processes.update(
            {row["pid"]: row for row in first_sample["process"]["processes"]}
        )
        sampler = threading.Thread(
            target=_sampler_loop,
            args=(sampler_stop, report["resource_samples"], report["sampler_errors"], runtime, process.pid, sidecar_pid, port),
            daemon=True,
        )
        sampler.start()
        _run_operator_actions(report, started)
        sampler_stop.set()
        sampler.join(timeout=SAMPLE_INTERVAL_SECONDS + 10)
        final_sample = resource_snapshot(runtime, process.pid, sidecar_pid, port)
        report["resource_samples"].append(final_sample)
        for sample in report["resource_samples"]:
            seen_processes.update({row["pid"]: row for row in sample["process"]["processes"]})
        report["database_corroboration"] = database_corroboration(
            before_database, database_snapshot(database)
        )
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:240]}
    finally:
        sampler_stop.set()
        if sampler is not None and sampler.is_alive():
            sampler.join(timeout=SAMPLE_INTERVAL_SECONDS + 10)
        if process is not None:
            if desktop_identity is None:
                try:
                    desktop_identity = _identity_for_pid(process.pid)
                except EnduranceEvidenceError:
                    desktop_identity = None
            if desktop_identity is None:
                report["cleanup"] = {
                    "owned_processes_released": process.poll() is not None,
                    "runtime_removed": False,
                    "external_processes_protected": False,
                    "forced": False,
                    "owned_gpu_mib_after": None,
                    "global_gpu_delta_mib": MAX_FINAL_GPU_DELTA_MIB + 1,
                    "ownership_lost": True,
                }
            else:
                seen_processes.setdefault(process.pid, desktop_identity)
                report["cleanup"] = _cleanup_observation(
                    process,
                    desktop_identity,
                    seen_processes,
                    runtime,
                    baseline_gpu,
                    external_ollama_before,
                )
        elif runtime.exists():
            removal = cleanup_runtime(runtime)
            report["cleanup"] = {
                "owned_processes_released": process is None or process.poll() is not None,
                "runtime_removed": removal["removed"],
                "runtime_cleanup_reason": removal["reason"],
                "external_processes_protected": named_process_identities("ollama.exe") == external_ollama_before,
                "forced": False,
                "owned_gpu_mib_after": 0,
                "global_gpu_delta_mib": 0,
            }
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        assessment = evaluate_report(report)
        report["checks"] = assessment["checks"]
        report["results"] = {
            "action_counts": assessment["counts"],
            "resource_assessment": assessment["resources"],
            "failure_rate_trend": [
                {
                    "sequence": event["sequence"],
                    "failures": sum(
                        prior.get("outcome") != "PASS"
                        for prior in report["events"][: index + 1]
                    ),
                    "failure_rate": round(
                        sum(
                            prior.get("outcome") != "PASS"
                            for prior in report["events"][: index + 1]
                        )
                        / (index + 1),
                        6,
                    ),
                }
                for index, event in enumerate(report["events"])
            ],
        }
        report["status"] = assessment["status"]
        report["finished_at"] = utc_now()
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect real operator-assisted v14 A23 endurance evidence.")
    parser.add_argument("--candidate-exe", required=True, help="Real packaged 司忆.exe candidate.")
    parser.add_argument("--output", required=True, help="Fresh JSON path under build/v1400-evidence.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Explicitly launch the candidate and begin the non-shortenable 30-minute operator run.",
    )
    arguments = parser.parse_args(argv)
    if not arguments.execute:
        parser.error("--execute is required; A23 cannot be inferred or simulated")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        output = resolve_output_path(arguments.output)
        candidate = resolve_candidate(arguments.candidate_exe)
        if output.exists():
            raise EnduranceEvidenceError(f"refusing to overwrite evidence: {output.name}")
        report = run_live_evidence(candidate, output)
        write_json_immutable(output, report)
    except (EnduranceEvidenceError, OSError) as exc:
        print(f"v14 A23 endurance evidence failed before completion: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "output": output.relative_to(ROOT).as_posix()}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
