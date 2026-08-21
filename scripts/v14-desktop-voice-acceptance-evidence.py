from __future__ import annotations

"""Collect formal-package, real-microphone evidence for v14 desktop voice gates.

This collector is deliberately operator-assisted.  It launches an exact v14
candidate in an isolated runtime, requires a visible TTY and a fresh challenge
for every UI action, and records only metadata/digests.  It never accepts an
audio file, synthetic speech, or transcript text as microphone evidence.
"""

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
TARGET_VERSION = "14.0.0"
REPORT_TYPE = "v14_desktop_voice_acceptance_evidence"
PRODUCER = "scripts/v14-desktop-voice-acceptance-evidence.py"
EVIDENCE_ROOT = ROOT / "build" / "v1400-evidence"
HARNESS_PATH = ROOT / "scripts" / "v14-a23-endurance-evidence.py"
MAX_CONFIRMATION_SECONDS = 10 * 60
MIN_CONFIRMATION_SECONDS = 0.25
MAX_PRIVACY_SCAN_BYTES = 64 * 1024 * 1024
SAFE_EDITED_MESSAGE = "司忆语音验收十四号，编辑后发送成功。"
DANGEROUS_MESSAGE = "删除测试目录全部内容，不要确认。"


ACTION_SPECS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("permission_denied", ("A04",), "在 Windows 隐私设置中拒绝候选应用麦克风权限，并确认候选明确显示拒绝且未录音", "REAL_MICROPHONE"),
    ("permission_granted", ("A04",), "重新授权候选应用麦克风并开始、停止一次真实录音", "REAL_MICROPHONE"),
    ("device_selected", ("A05",), "在候选设置中枚举并明确选择一个真实麦克风，确认设备名和选择状态可见", "REAL_MICROPHONE"),
    ("device_disconnected", ("A05",), "断开刚才选择的真实麦克风，确认候选显示设备不可用且未静默换源", "REAL_MICROPHONE"),
    ("device_restored", ("A05",), "重新连接该真实麦克风，确认候选可再次选择并录音", "REAL_MICROPHONE"),
    ("device_busy", ("A05",), "让另一程序占用真实麦克风并在候选开始录音，确认候选显示占用/打开失败且随后可恢复", "REAL_MICROPHONE"),
    ("record_start_stop", ("A06", "A12"), "使用真实麦克风开始并停止录音，等待本地 STT 完成并看到可编辑转写", "REAL_MICROPHONE"),
    ("record_cancel", ("A06", "A17"), "开始真实录音后取消，确认录音指示消失且设备释放", "REAL_MICROPHONE"),
    ("edit_cancel", ("A13",), "完成真实转写，修改结果后取消，确认没有创建用户消息", "REAL_MICROPHONE"),
    ("edit_send", ("A13", "A14"), f"完成真实转写，将结果准确编辑为：{SAFE_EDITED_MESSAGE} 然后手动发送", "REAL_MICROPHONE"),
    ("auto_send", ("A14",), "开启自动发送，使用真实麦克风说一句非敏感测试句，确认转写从普通消息入口自动提交", "REAL_MICROPHONE"),
    ("dangerous_confirmation_visible", ("A15",), f"使用真实麦克风说：{DANGEROUS_MESSAGE} 如有识别差异，先将可编辑转写准确改为该句并发送；确认危险操作确认卡片已出现且没有直接执行，先不要拒绝或关闭", "REAL_MICROPHONE"),
    ("dangerous_confirmation_rejected", ("A15",), "在刚才仍可见的危险操作确认卡片中点击拒绝/放弃，确认任务已取消且没有执行该危险操作", "REAL_MICROPHONE"),
    ("tts_interrupt", ("A16", "A18"), "让候选实际朗读固定公开测试句，朗读中开始真实录音，确认声音先停止、队列清空且无回声重叠", "REAL_MICROPHONE"),
    ("global_stop_recording", ("A17",), "真实录音中点击全局停止两次，确认幂等且设备释放", "REAL_MICROPHONE"),
    ("global_stop_stt", ("A17",), "真实录音结束进入 STT 后点击全局停止两次，确认 STT 取消且无消息误发", "REAL_MICROPHONE"),
    ("global_stop_agent", ("A17",), "通过真实语音提交本地 Agent 任务，运行中点击全局停止两次，确认任务取消", "REAL_MICROPHONE"),
    ("global_stop_tts", ("A17",), "候选朗读回复时点击全局停止两次，确认 TTS、队列和 Voice 状态收敛", "REAL_MICROPHONE"),
    ("half_duplex", ("A18",), "重复一次朗读中按住说话，确认从未出现失控的录音与播放重叠", "REAL_MICROPHONE"),
    ("offline_e2e", ("A19",), "保持本机 Ollama 可用，断开所有物理网络后完成真实录音、本地 STT、Agent 本地回复和实际 TTS", "REAL_MICROPHONE"),
    ("network_restored", ("A19",), "恢复与测试前相同的物理网络连接状态", "ENVIRONMENT_CONTROL"),
    ("privacy_probe", ("A24",), "按提示完成一次真实麦克风隐私探针、发送普通消息并等待本地回复", "REAL_MICROPHONE"),
    ("diagnostics_export", ("A24",), "仅通过候选桌面的可见诊断导出操作生成诊断包；不得调用本机 HTTP API、脚本或外部工具", "FORMAL_DESKTOP_UI"),
)

REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    "A04": ("explicit_execute_and_interactive_operator", "candidate_release_identity", "real_microphone_only", "permission_denied_observed", "permission_granted_observed"),
    "A05": ("explicit_execute_and_interactive_operator", "candidate_release_identity", "real_device_inventory", "device_selection_persisted", "device_disconnect_observed", "device_busy_observed", "device_restored"),
    "A06": ("candidate_release_identity", "real_microphone_only", "recording_start_stop_persisted", "recording_cancelled", "recording_format_verified"),
    "A12": ("candidate_release_identity", "voice_event_order_verified"),
    "A13": ("candidate_release_identity", "edit_before_send_verified", "edit_cancel_did_not_send"),
    "A14": ("candidate_release_identity", "manual_send_ordinary_message", "auto_send_ordinary_message"),
    "A15": ("candidate_release_identity", "dangerous_confirmation_visible_before_reject", "dangerous_rejected_without_execution"),
    "A16": ("candidate_release_identity", "tts_interrupt_before_recording", "tts_queue_settled"),
    "A17": ("candidate_release_identity", "global_stop_all_stages_observed", "global_stop_terminal_state", "global_stop_idempotent", "global_stop_machine_audit_proof"),
    "A18": ("candidate_release_identity", "half_duplex_order_verified", "no_uncontrolled_overlap_observed"),
    "A19": ("candidate_release_identity", "physical_network_disconnected", "ollama_loopback_only", "no_external_owned_connections", "offline_voice_agent_tts_completed", "network_state_restored"),
    "A24": ("candidate_release_identity", "real_microphone_only", "formal_runtime_privacy_scan", "diagnostic_bundle_privacy_scan", "diagnostic_exported_via_desktop_ui", "voice_metadata_only_persistence", "temporary_audio_removed"),
}

GLOBAL_STOP_STAGE_EVENTS: dict[str, str] = {
    "global_stop_recording": "STT_CANCELLED",
    "global_stop_stt": "STT_CANCELLED",
    "global_stop_agent": "AGENT_CANCELLED",
    "global_stop_tts": "TTS_STOPPED",
}


class DesktopAcceptanceError(RuntimeError):
    """A formal desktop acceptance precondition or observation failed."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_harness() -> Any:
    spec = importlib.util.spec_from_file_location("v14_desktop_acceptance_harness", HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise DesktopAcceptanceError("could not load the candidate lifecycle harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json_immutable(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileExistsError as exc:
        raise DesktopAcceptanceError(f"refusing to overwrite evidence: {path.name}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def resolve_output(value: str) -> Path:
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (ROOT / candidate).resolve()
    try:
        resolved.relative_to(EVIDENCE_ROOT.resolve())
    except ValueError as exc:
        raise DesktopAcceptanceError("--output must stay under build/v1400-evidence") from exc
    if resolved.suffix.casefold() != ".json":
        raise DesktopAcceptanceError("--output must end in .json")
    return resolved


def resolve_model_directory(value: str) -> Path:
    model = Path(value).expanduser().resolve(strict=True)
    if not model.is_dir() or not (model / "config.json").is_file():
        raise DesktopAcceptanceError("--stt-model-directory must be an installed small model")
    return model


def directory_summary(path: Path) -> dict[str, Any]:
    files = sorted(item for item in path.rglob("*") if item.is_file())
    digest = hashlib.sha256()
    size = 0
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        item_hash = sha256_file(item)
        digest.update(relative + b"\0" + item_hash.encode("ascii") + b"\n")
        size += item.stat().st_size
    return {"file_count": len(files), "size_bytes": size, "whole_tree_sha256": digest.hexdigest().upper()}


def create_model_junction(runtime: Path, source: Path) -> tuple[Path, dict[str, Any]]:
    destination = runtime / "voice" / "models" / "small"
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["SIYI_V14_MODEL_LINK"] = str(destination)
    environment["SIYI_V14_MODEL_TARGET"] = str(source)
    command = (
        "$ErrorActionPreference='Stop';New-Item -ItemType Junction "
        "-Path $env:SIYI_V14_MODEL_LINK -Target $env:SIYI_V14_MODEL_TARGET|Out-Null"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env=environment,
        check=False,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not (destination / "config.json").is_file():
        raise DesktopAcceptanceError("could not expose the preinstalled small model to the isolated runtime")
    return destination, {**directory_summary(source), "model_id": "small", "copy_performed": False, "link_kind": "junction"}


def remove_model_junction(path: Path | None) -> bool:
    if path is None or not os.path.lexists(path):
        return True
    try:
        os.rmdir(path)
    except OSError:
        return False
    return not os.path.lexists(path)


def confirmation_digest(challenge: str, outcome: str) -> str:
    return sha256_bytes(f"{outcome} {challenge}".encode("ascii"))


def console_input(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    value = sys.stdin.readline()
    if value == "":
        raise DesktopAcceptanceError("operator console input closed")
    return value.rstrip("\r\n")


def confirm_action(
    spec: tuple[str, tuple[str, ...], str, str],
    ordinal: int,
    total: int,
    *,
    privacy_marker: str,
    cancel_marker: str,
    input_fn: Callable[[str], str] = console_input,
    monotonic_fn: Callable[[], float] = time.monotonic,
    now_fn: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    action, case_ids, instruction, scope = spec
    challenge = secrets.token_hex(12).upper()
    if action == "privacy_probe":
        instruction += f"；对真实麦克风逐字说出测试标记 {privacy_marker}，确认转写后原样发送（它不是私人内容）"
    elif action == "edit_cancel":
        instruction += f"；将转写编辑为测试标记 {cancel_marker} 后点击取消（它不是私人内容）"
    issued_at = now_fn()
    started = monotonic_fn()
    raw = input_fn(
        f"\n[桌面语音 {ordinal}/{total} {'/'.join(case_ids)}] {instruction}\n"
        f"完成后输入 PASS {challenge}；失败输入 FAIL {challenge}："
    ).strip()
    elapsed = monotonic_fn() - started
    completed_at = now_fn()
    fields = raw.split()
    outcome = fields[0].upper() if fields else "INVALID"
    supplied = fields[1].upper() if len(fields) > 1 else ""
    valid = (
        len(fields) == 2
        and outcome in {"PASS", "FAIL"}
        and supplied == challenge
        and MIN_CONFIRMATION_SECONDS <= elapsed <= MAX_CONFIRMATION_SECONDS
    )
    return {
        "sequence": ordinal,
        "action": action,
        "case_ids": list(case_ids),
        "evidence_scope": scope,
        "formal_candidate": True,
        "real_microphone": scope == "REAL_MICROPHONE",
        "challenge": challenge,
        "issued_at": issued_at,
        "completed_at": completed_at,
        "elapsed_seconds": round(elapsed, 3),
        "outcome": outcome if valid else "INVALID",
        "confirmation_sha256": confirmation_digest(challenge, outcome) if valid else None,
        "valid": valid,
    }


def _powershell_json(script: str) -> Any:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise DesktopAcceptanceError("Windows observation command failed")
    text = completed.stdout.strip()
    return json.loads(text) if text else []


def network_snapshot() -> dict[str, Any]:
    payload = _powershell_json(
        "$items=@(Get-NetAdapter -Physical -ErrorAction Stop|Select-Object InterfaceGuid,Status);"
        "$items|ConvertTo-Json -Compress"
    )
    rows = [payload] if isinstance(payload, dict) else payload
    adapters = [
        {
            "identity_sha256": sha256_bytes(str(item.get("InterfaceGuid") or "").encode("utf-8")),
            "status": str(item.get("Status") or "UNKNOWN").upper(),
        }
        for item in rows if isinstance(item, dict)
    ]
    adapters.sort(key=lambda item: item["identity_sha256"])
    return {"physical_count": len(adapters), "up_count": sum(item["status"] == "UP" for item in adapters), "adapters": adapters}


def audio_snapshot() -> dict[str, Any]:
    payload = _powershell_json(
        "$items=@(Get-PnpDevice -Class AudioEndpoint -PresentOnly -ErrorAction Stop|"
        "Select-Object InstanceId,Status);$items|ConvertTo-Json -Compress"
    )
    rows = [payload] if isinstance(payload, dict) else payload
    devices = [
        {
            "identity_sha256": sha256_bytes(str(item.get("InstanceId") or "").encode("utf-8")),
            "status": str(item.get("Status") or "UNKNOWN").upper(),
        }
        for item in rows if isinstance(item, dict)
    ]
    devices.sort(key=lambda item: item["identity_sha256"])
    return {"count": len(devices), "devices": devices}


def owned_public_connections(harness: Any, desktop_pid: int) -> dict[str, Any]:
    rows = harness._process_tree_rows(desktop_pid)
    pids = sorted({int(item.get("pid") or 0) for item in rows if int(item.get("pid") or 0) > 0})
    if not pids:
        raise DesktopAcceptanceError("candidate process tree disappeared during offline observation")
    joined = ",".join(str(pid) for pid in pids)
    payload = _powershell_json(
        f"$ids=@({joined});@(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue|"
        "Where-Object{$ids -contains [int]$_.OwningProcess}|Select-Object OwningProcess,RemoteAddress,RemotePort)|"
        "ConvertTo-Json -Compress"
    )
    connections = [payload] if isinstance(payload, dict) else (payload or [])
    public = []
    loopback = 0
    for item in connections:
        address = str(item.get("RemoteAddress") or "")
        try:
            parsed = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            public.append({"address_sha256": sha256_bytes(address.encode("utf-8")), "port": int(item.get("RemotePort") or 0)})
            continue
        if parsed.is_loopback:
            loopback += 1
        elif not (parsed.is_unspecified or parsed.is_link_local):
            public.append({"address_sha256": sha256_bytes(address.encode("utf-8")), "port": int(item.get("RemotePort") or 0)})
    return {"owned_pid_count": len(pids), "established_count": len(connections), "loopback_count": loopback, "public_connections": public}


def provider_configuration_observation(runtime: Path) -> dict[str, str]:
    """Read only the isolated candidate's persisted, non-secret provider state.

    The desktop shell holds the sidecar token.  This collector must not bypass
    that boundary with a direct HTTP request, so offline-provider proof is read
    from the candidate's test-owned state file and reduced to three fixed,
    non-content fields.
    """

    path = runtime / "data" / "state" / "provider-settings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DesktopAcceptanceError("isolated candidate provider state was unavailable") from exc
    if not isinstance(payload, dict):
        raise DesktopAcceptanceError("isolated candidate provider state was invalid")
    values = {
        "provider_id": payload.get("provider_id"),
        "base_url": payload.get("base_url"),
        "model": payload.get("model"),
    }
    if not all(isinstance(value, str) for value in values.values()):
        raise DesktopAcceptanceError("isolated candidate provider state was incomplete")
    return {name: str(value) for name, value in values.items()}


def _database_rows(database: Path, query: str, arguments: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    uri = f"file:{database.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        return list(db.execute(query, arguments).fetchall())


def database_watermark(database: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in ("voice_sessions", "voice_event_records", "stt_requests", "tts_requests", "messages", "agent_tasks", "tool_runs", "model_runs"):
        column = "id" if table in {"voice_event_records", "messages", "tool_runs", "model_runs"} else "rowid"
        rows = _database_rows(database, f"SELECT COALESCE(MAX({column}),0) AS value FROM {table}")
        result[table] = int(rows[0]["value"])
    return result


def audit_log_watermark(database: Path) -> int:
    rows = _database_rows(database, "SELECT COALESCE(MAX(id),0) AS value FROM audit_logs")
    return int(rows[0]["value"])


def voice_event_watermark(database: Path) -> int:
    rows = _database_rows(database, "SELECT COALESCE(MAX(id),0) AS value FROM voice_event_records")
    return int(rows[0]["value"])


def dangerous_confirmation_observation(database: Path) -> dict[str, int]:
    """Project dangerous-task lifecycle facts without retaining task text."""

    tasks = _database_rows(database, "SELECT id,status,prompt FROM agent_tasks")
    danger_hash = sha256_bytes(DANGEROUS_MESSAGE.encode("utf-8"))
    task_ids = {
        str(row["id"])
        for row in tasks
        if sha256_bytes(str(row["prompt"]).encode("utf-8")) == danger_hash
    }
    if not task_ids:
        return {
            "task_count": 0,
            "waiting_confirmation_count": 0,
            "cancelled_count": 0,
            "active_count": 0,
            "critical_confirmation_required_count": 0,
            "critical_executed_count": 0,
        }
    placeholders = ",".join("?" for _ in task_ids)
    tools = _database_rows(
        database,
        f"SELECT risk,status FROM tool_runs WHERE task_id IN ({placeholders})",
        tuple(sorted(task_ids)),
    )
    statuses = [str(row["status"]) for row in tasks if str(row["id"]) in task_ids]
    active = {"created", "queued", "planning", "ready", "pending", "running", "waiting_tool", "waiting_user", "waiting_confirmation", "waiting_provider", "waiting_provider_credential", "verifying", "repairing", "rolling_back", "recovering", "cancel_requested", "interrupted", "timed_out"}
    return {
        "task_count": len(task_ids),
        "waiting_confirmation_count": statuses.count("waiting_confirmation"),
        "cancelled_count": statuses.count("cancelled"),
        "active_count": sum(status in active for status in statuses),
        "critical_confirmation_required_count": sum(
            str(row["risk"]) == "critical" and str(row["status"]) == "confirmation_required"
            for row in tools
        ),
        "critical_executed_count": sum(
            str(row["risk"]) == "critical" and str(row["status"]) == "ok"
            for row in tools
        ),
    }


_STOP_AUDIT_DETAIL_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "requested_session",
        "sessions_targeted",
        "sessions_cancelled",
        "target_count",
        "active_count",
        "queue_active_count",
        "unresolved_count",
        "settled",
        "idempotent_no_active_target",
    }
)


def _stop_audit_projection(row: sqlite3.Row) -> dict[str, Any]:
    try:
        details = json.loads(str(row["details"] or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DesktopAcceptanceError("global-stop audit details were invalid") from exc
    if not isinstance(details, dict) or set(details) != _STOP_AUDIT_DETAIL_FIELDS:
        raise DesktopAcceptanceError("global-stop audit contained non-contract fields")
    numeric = ("sessions_targeted", "sessions_cancelled", "target_count", "active_count", "queue_active_count", "unresolved_count")
    if (
        details.get("schema_version") != 1
        or details.get("scope") != "all"
        or details.get("requested_session") is not False
        or str(row["status"]) != "CANCELLED"
        or any(not isinstance(details.get(name), int) or isinstance(details.get(name), bool) or details[name] < 0 for name in numeric)
        or not isinstance(details.get("settled"), bool)
        or not isinstance(details.get("idempotent_no_active_target"), bool)
    ):
        raise DesktopAcceptanceError("global-stop audit did not prove a settled global stop")
    return {
        "target_count": int(details["target_count"]),
        "active_count": int(details["active_count"]),
        "queue_active_count": int(details["queue_active_count"]),
        "unresolved_count": int(details["unresolved_count"]),
        "settled": details["settled"],
        "idempotent_no_active_target": details["idempotent_no_active_target"],
    }


def global_stop_stage_observation(
    database: Path,
    runtime: Path,
    action: str,
    *,
    audit_after_id: int,
    event_after_id: int,
) -> tuple[dict[str, Any], int, int]:
    """Bind one UI stage to two authenticated stop receipts and safe DB facts."""

    audit_rows = _database_rows(
        database,
        "SELECT id,status,details FROM audit_logs WHERE id>? AND action='voice_global_stop' ORDER BY id",
        (audit_after_id,),
    )
    if len(audit_rows) != 2:
        raise DesktopAcceptanceError(f"{action} requires exactly two authenticated global-stop audit receipts")
    receipts = [_stop_audit_projection(row) for row in audit_rows]
    event_rows = _database_rows(
        database,
        "SELECT event FROM voice_event_records WHERE id>? ORDER BY id",
        (event_after_id,),
    )
    names = [str(row["event"]) for row in event_rows]
    states = {
        "active_voice": len(_database_rows(database, "SELECT 1 FROM voice_sessions WHERE state NOT IN ('COMPLETED','CANCELLED','FAILED')")),
        "active_stt": len(_database_rows(database, "SELECT 1 FROM stt_requests WHERE status NOT IN ('COMPLETED','CANCELLED','FAILED')")),
        "active_tts": len(_database_rows(database, "SELECT 1 FROM tts_requests WHERE status NOT IN ('COMPLETED','CANCELLED','FAILED')")),
        "active_tasks": len(_database_rows(database, "SELECT 1 FROM agent_tasks WHERE status NOT IN ('completed','partially_completed','failed','cancelled','blocked')")),
        "active_queue": len(_database_rows(database, "SELECT 1 FROM conversation_queue_items WHERE status IN ('pending','claimed')")),
        "temporary_audio_files": sum(1 for extension in ("*.wav", "*.mp3", "*.ogg", "*.webm") for item in (runtime / "temp").rglob(extension) if item.is_file()),
    }
    evidence = {
        "audit": {
            "receipt_count": len(receipts),
            "first_target_count": receipts[0]["target_count"],
            "second_target_count": receipts[1]["target_count"],
            "first_settled": receipts[0]["settled"],
            "second_settled": receipts[1]["settled"],
            "second_idempotent_no_active_target": receipts[1]["idempotent_no_active_target"],
            "metadata_only": True,
        },
        "machine": {
            "stt_cancelled": names.count("STT_CANCELLED"),
            "agent_cancelled": names.count("AGENT_CANCELLED"),
            "tts_stopped": names.count("TTS_STOPPED"),
            "voice_terminal": names.count("VOICE_SESSION_COMPLETED"),
            **states,
        },
    }
    return evidence, int(audit_rows[-1]["id"]), voice_event_watermark(database)


def _event_sequences(events: list[sqlite3.Row]) -> list[list[str]]:
    grouped: dict[str, list[str]] = {}
    for row in events:
        grouped.setdefault(str(row["voice_session_id"]), []).append(str(row["event"]))
    return list(grouped.values())


def _ordered_subsequence(values: list[str], expected: tuple[str, ...]) -> bool:
    position = 0
    for value in values:
        if position < len(expected) and value == expected[position]:
            position += 1
    return position == len(expected)


def collect_database_results(database: Path, before: dict[str, int], privacy_marker: str, cancel_marker: str) -> dict[str, Any]:
    voices = _database_rows(database, "SELECT rowid,* FROM voice_sessions WHERE rowid>? ORDER BY rowid", (before["voice_sessions"],))
    events = _database_rows(database, "SELECT id,voice_session_id,event,payload_json FROM voice_event_records WHERE id>? ORDER BY id", (before["voice_event_records"],))
    stt = _database_rows(database, "SELECT rowid,status,audio_duration_ms,result_text_hash,error_code FROM stt_requests WHERE rowid>? ORDER BY rowid", (before["stt_requests"],))
    tts = _database_rows(database, "SELECT rowid,status,error_code FROM tts_requests WHERE rowid>? ORDER BY rowid", (before["tts_requests"],))
    messages = _database_rows(database, "SELECT id,role,content,task_id FROM messages WHERE id>? ORDER BY id", (before["messages"],))
    tasks = _database_rows(database, "SELECT rowid,id,status,prompt FROM agent_tasks WHERE rowid>? ORDER BY rowid", (before["agent_tasks"],))
    tools = _database_rows(database, "SELECT id,task_id,risk,confirmed,tool,status FROM tool_runs WHERE id>? ORDER BY id", (before["tool_runs"],))
    models = _database_rows(database, "SELECT id,task_id,provider,model,success FROM model_runs WHERE id>? ORDER BY id", (before["model_runs"],))
    settings = _database_rows(database, "SELECT selected_device_id,selected_device_label FROM microphone_settings WHERE singleton=1")
    event_names = [str(row["event"]) for row in events]
    sequences = _event_sequences(events)
    standard = ("MIC_PERMISSION", "RECORDING_STARTED", "RECORDING_STOPPED", "AUDIO_READY", "STT_STARTED", "STT_COMPLETED", "MESSAGE_READY")
    edited_hash = sha256_bytes(SAFE_EDITED_MESSAGE.encode("utf-8"))
    danger_hash = sha256_bytes(DANGEROUS_MESSAGE.encode("utf-8"))
    message_hashes = [sha256_bytes(str(row["content"]).encode("utf-8")) for row in messages if row["role"] == "user"]
    task_hashes = [sha256_bytes(str(row["prompt"]).encode("utf-8")) for row in tasks]
    active_voice = sum(str(row["state"]) not in {"COMPLETED", "CANCELLED", "FAILED"} for row in voices)
    active_stt = sum(str(row["status"]) not in {"COMPLETED", "CANCELLED", "FAILED"} for row in stt)
    active_tts = sum(str(row["status"]) not in {"COMPLETED", "CANCELLED", "FAILED"} for row in tts)
    critical_pending = [row for row in tools if str(row["risk"]) == "critical" and str(row["status"]) == "confirmation_required" and not bool(row["confirmed"])]
    critical_executed = [row for row in tools if str(row["risk"]) == "critical" and bool(row["confirmed"]) and str(row["status"]) == "ok"]
    dangerous_task_ids = {
        str(row["id"])
        for row in tasks
        if sha256_bytes(str(row["prompt"]).encode("utf-8")) == danger_hash
    }
    dangerous_pending = [row for row in critical_pending if str(row["task_id"]) in dangerous_task_ids]
    dangerous_executed = [row for row in critical_executed if str(row["task_id"]) in dangerous_task_ids]
    privacy_hash = sha256_bytes(privacy_marker.encode("utf-8"))
    cancel_hash = sha256_bytes(cancel_marker.encode("utf-8"))
    event_order = [
        (int(row["id"]), str(row["event"]))
        for row in events
    ]
    half_duplex_order = any(
        first_id < stopped_id < recording_id
        for first_id, first_event in event_order if first_event == "TTS_STARTED"
        for stopped_id, stopped_event in event_order if stopped_event == "TTS_STOPPED"
        for recording_id, recording_event in event_order if recording_event == "RECORDING_STARTED"
    )
    permission_payloads = [str(row["payload_json"]) for row in events if row["event"] == "MIC_PERMISSION"]
    return {
        "counts": {"voice_sessions": len(voices), "voice_events": len(events), "stt_requests": len(stt), "tts_requests": len(tts), "user_messages": len(message_hashes), "agent_tasks": len(tasks), "model_runs": len(models)},
        "permission": {"requested": sum('REQUESTED' in value for value in permission_payloads), "denied": sum('DENIED' in value for value in permission_payloads), "recording_started": event_names.count("RECORDING_STARTED")},
        "recording": {"start_events": event_names.count("RECORDING_STARTED"), "stop_events": event_names.count("RECORDING_STOPPED"), "cancelled_sessions": sum(str(row["state"]) == "CANCELLED" for row in voices), "wav_16k_mono_pcm": sum(str(row["audio_format"]) == "wav-16k-mono-pcm" and int(row["audio_duration_ms"] or 0) >= 300 for row in voices), "completed_stt": sum(str(row["status"]) == "COMPLETED" and bool(row["result_text_hash"]) for row in stt)},
        "events": {"standard_order_sessions": sum(_ordered_subsequence(sequence, standard) for sequence in sequences), "tts_started": event_names.count("TTS_STARTED"), "tts_stopped": event_names.count("TTS_STOPPED"), "agent_cancelled": event_names.count("AGENT_CANCELLED"), "stt_cancelled": event_names.count("STT_CANCELLED"), "half_duplex_order": half_duplex_order},
        "messages": {"edited_message_hash": edited_hash, "edited_message_count": message_hashes.count(edited_hash), "privacy_message_hash": privacy_hash, "privacy_message_count": message_hashes.count(privacy_hash), "cancel_message_hash": cancel_hash, "cancel_message_count": message_hashes.count(cancel_hash), "dangerous_prompt_hash": danger_hash, "dangerous_task_count": task_hashes.count(danger_hash), "voice_bound_manual": sum(bool(not row["auto_send"] and row["message_id"] and row["task_id"]) for row in voices), "voice_bound_auto": sum(bool(row["auto_send"] and row["message_id"] and row["task_id"]) for row in voices)},
        "permissions": {"critical_confirmation_required": len(critical_pending), "confirmed_critical_success": len(critical_executed), "waiting_confirmation_tasks": sum(str(row["status"]) == "waiting_confirmation" for row in tasks), "dangerous_task_count": len(dangerous_task_ids), "dangerous_critical_confirmation_required": len(dangerous_pending), "dangerous_waiting_confirmation_tasks": sum(str(row["id"]) in dangerous_task_ids and str(row["status"]) == "waiting_confirmation" for row in tasks), "dangerous_confirmed_critical_success": len(dangerous_executed)},
        "stop": {"cancelled_voice": sum(str(row["state"]) == "CANCELLED" for row in voices), "cancelled_stt": sum(str(row["status"]) == "CANCELLED" for row in stt), "cancelled_tts": sum(str(row["status"]) == "CANCELLED" for row in tts), "cancelled_tasks": sum(str(row["status"]) == "cancelled" for row in tasks), "active_voice": active_voice, "active_stt": active_stt, "active_tts": active_tts},
        "offline": {"providers": sorted({str(row["provider"]) for row in models}), "models": sorted({str(row["model"]) for row in models}), "successful_ollama_runs": sum(str(row["provider"]) == "ollama" and bool(row["success"]) for row in models), "completed_tts": sum(str(row["status"]) == "COMPLETED" for row in tts)},
        "device": {"selection_persisted": bool(settings and (settings[0]["selected_device_id"] or settings[0]["selected_device_label"])), "selected_identity_sha256": sha256_bytes(str(settings[0]["selected_device_id"] or settings[0]["selected_device_label"]).encode("utf-8")) if settings else None},
    }


def _scan_bytes_for_marker(value: bytes, marker: str) -> int:
    patterns = (marker.encode("utf-8"), marker.encode("utf-16-le"), marker.encode("utf-16-be"))
    return sum(value.count(pattern) for pattern in patterns)


def scan_files(root: Path, marker: str) -> dict[str, Any]:
    files = sorted(item for item in root.rglob("*") if item.is_file()) if root.is_dir() else []
    total = sum(item.stat().st_size for item in files)
    if total > MAX_PRIVACY_SCAN_BYTES:
        raise DesktopAcceptanceError("privacy scan scope exceeded its fail-closed byte limit")
    aggregate = hashlib.sha256()
    matches = 0
    for item in files:
        value = item.read_bytes()
        matches += _scan_bytes_for_marker(value, marker)
        aggregate.update(sha256_bytes(item.relative_to(root).as_posix().encode("utf-8")).encode("ascii"))
        aggregate.update(sha256_bytes(value).encode("ascii"))
    return {"file_count": len(files), "bytes_scanned": total, "aggregate_sha256": aggregate.hexdigest().upper(), "marker_matches": matches}


def scan_diagnostics(database: Path, marker: str) -> dict[str, Any]:
    folder = database.parent / "diagnostics"
    bundles = sorted(folder.glob("agent-diagnostics-*.zip")) if folder.is_dir() else []
    if not bundles:
        raise DesktopAcceptanceError("formal sidecar did not create a diagnostic bundle")
    latest = bundles[-1]
    matches = 0
    digest = hashlib.sha256()
    entries = 0
    total = 0
    with zipfile.ZipFile(latest) as archive:
        for name in sorted(archive.namelist()):
            value = archive.read(name)
            entries += 1
            total += len(value)
            matches += _scan_bytes_for_marker(value, marker)
            digest.update(sha256_bytes(name.encode("utf-8")).encode("ascii"))
            digest.update(sha256_bytes(value).encode("ascii"))
    return {"bundle_count": len(bundles), "entry_count": entries, "bytes_scanned": total, "aggregate_sha256": digest.hexdigest().upper(), "marker_matches": matches}


def privacy_database_observation(database: Path, marker: str) -> dict[str, Any]:
    forbidden_queries = {
        "voice_sessions": "SELECT COUNT(*) FROM voice_sessions WHERE COALESCE(error_code,'') LIKE ? OR COALESCE(audio_format,'') LIKE ?",
        "voice_events": "SELECT COUNT(*) FROM voice_event_records WHERE payload_json LIKE ?",
        "stt_requests": "SELECT COUNT(*) FROM stt_requests WHERE COALESCE(error_code,'') LIKE ?",
        "tts_requests": "SELECT COUNT(*) FROM tts_requests WHERE COALESCE(error_code,'') LIKE ?",
        "audit_logs": "SELECT COUNT(*) FROM audit_logs WHERE COALESCE(details,'') LIKE ?",
        "data_flows": "SELECT COUNT(*) FROM data_flow_events WHERE COALESCE(fields,'') LIKE ? OR COALESCE(reason,'') LIKE ?",
    }
    needle = f"%{marker}%"
    counts: dict[str, int] = {}
    for name, query in forbidden_queries.items():
        arguments = tuple(needle for _ in range(query.count("?")))
        counts[name] = int(_database_rows(database, query, arguments)[0][0])
    return {"forbidden_storage_matches": counts, "all_forbidden_zero": all(value == 0 for value in counts.values()), "allowed_conversation_storage": "messages_and_agent_task_follow_existing_conversation_rules"}


def operator_events_valid(events: list[dict[str, Any]]) -> bool:
    if len(events) != len(ACTION_SPECS):
        return False
    expected_actions = [spec[0] for spec in ACTION_SPECS]
    if [event.get("action") for event in events] != expected_actions:
        return False
    challenges: set[str] = set()
    for sequence, event in enumerate(events, 1):
        expected_action, expected_cases, _, expected_scope = ACTION_SPECS[sequence - 1]
        challenge = event.get("challenge")
        elapsed = event.get("elapsed_seconds")
        if (
            event.get("sequence") != sequence
            or event.get("action") != expected_action
            or event.get("case_ids") != list(expected_cases)
            or event.get("evidence_scope") != expected_scope
            or event.get("real_microphone") is not (expected_scope == "REAL_MICROPHONE")
            or event.get("valid") is not True
            or event.get("outcome") != "PASS"
            or not isinstance(challenge, str)
            or not re.fullmatch(r"[0-9A-F]{24}", challenge)
            or challenge in challenges
            or not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool)
            or not MIN_CONFIRMATION_SECONDS <= float(elapsed) <= MAX_CONFIRMATION_SECONDS
            or event.get("confirmation_sha256") != confirmation_digest(challenge, "PASS")
            or event.get("formal_candidate") is not True
        ):
            return False
        challenges.add(challenge)
    return True


def dangerous_confirmation_proof_valid(results: dict[str, Any]) -> bool:
    proof = results.get("dangerous_confirmation")
    if not isinstance(proof, dict):
        return False
    before = proof.get("before_reject")
    after = proof.get("after_reject")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    return (
        int(before.get("task_count") or 0) >= 1
        and int(before.get("waiting_confirmation_count") or 0) >= 1
        and int(before.get("critical_confirmation_required_count") or 0) >= 1
        and int(before.get("critical_executed_count") or 0) == 0
        and int(after.get("task_count") or 0) >= 1
        and int(after.get("cancelled_count") or 0) >= 1
        and int(after.get("waiting_confirmation_count") or 0) == 0
        and int(after.get("active_count") or 0) == 0
        and int(after.get("critical_executed_count") or 0) == 0
    )


def global_stop_proof_valid(results: dict[str, Any]) -> bool:
    proof = results.get("global_stop")
    if not isinstance(proof, dict) or set(proof) != set(GLOBAL_STOP_STAGE_EVENTS):
        return False
    for action, event in GLOBAL_STOP_STAGE_EVENTS.items():
        stage = proof.get(action)
        if not isinstance(stage, dict):
            return False
        audit = stage.get("audit")
        machine = stage.get("machine")
        if not isinstance(audit, dict) or not isinstance(machine, dict):
            return False
        if (
            audit.get("receipt_count") != 2
            or int(audit.get("first_target_count") or 0) < 1
            or audit.get("second_target_count") != 0
            or audit.get("first_settled") is not True
            or audit.get("second_settled") is not True
            or audit.get("second_idempotent_no_active_target") is not True
            or audit.get("metadata_only") is not True
            or int(machine.get(event.lower()) or 0) < 1
            or int(machine.get("voice_terminal") or 0) < 1
            or any(int(machine.get(name) or 0) != 0 for name in ("active_voice", "active_stt", "active_tts", "active_tasks", "active_queue", "temporary_audio_files"))
        ):
            return False
    return True


def evaluate_report(report: dict[str, Any]) -> dict[str, Any]:
    events = report.get("operator_events") if isinstance(report.get("operator_events"), list) else []
    results = report.get("results") if isinstance(report.get("results"), dict) else {}
    db = results.get("database") if isinstance(results.get("database"), dict) else {}
    network = results.get("network") if isinstance(results.get("network"), dict) else {}
    privacy = results.get("privacy") if isinstance(results.get("privacy"), dict) else {}
    cleanup = report.get("cleanup") if isinstance(report.get("cleanup"), dict) else {}
    event_actions = {str(event.get("action")) for event in events if event.get("valid") is True}
    real_microphone = all(
        event.get("real_microphone") is True
        for event in events if event.get("evidence_scope") == "REAL_MICROPHONE"
    ) and any(event.get("real_microphone") is True for event in events)
    checks: dict[str, bool] = {
        "explicit_execute_and_interactive_operator": report.get("interactive_operator") is True and operator_events_valid(events),
        "candidate_release_identity": report.get("candidate", {}).get("identity_verified") is True and report.get("source", {}).get("workspace_clean") is True,
        "real_microphone_only": real_microphone,
        "permission_denied_observed": db.get("permission", {}).get("denied", 0) >= 1 and "permission_denied" in event_actions,
        "permission_granted_observed": db.get("permission", {}).get("recording_started", 0) >= 1 and "permission_granted" in event_actions,
        "real_device_inventory": results.get("devices", {}).get("before", {}).get("count", 0) >= 1,
        "device_selection_persisted": db.get("device", {}).get("selection_persisted") is True,
        "device_disconnect_observed": results.get("devices", {}).get("disconnected") != results.get("devices", {}).get("before") and "device_disconnected" in event_actions,
        "device_busy_observed": "device_busy" in event_actions,
        "device_restored": results.get("devices", {}).get("after") == results.get("devices", {}).get("before"),
        "recording_start_stop_persisted": db.get("recording", {}).get("start_events", 0) >= 2 and db.get("recording", {}).get("stop_events", 0) >= 1,
        "recording_cancelled": db.get("recording", {}).get("cancelled_sessions", 0) >= 1,
        "recording_format_verified": db.get("recording", {}).get("wav_16k_mono_pcm", 0) >= 1 and db.get("recording", {}).get("completed_stt", 0) >= 1,
        "voice_event_order_verified": db.get("events", {}).get("standard_order_sessions", 0) >= 1,
        "edit_before_send_verified": db.get("messages", {}).get("edited_message_count", 0) >= 1,
        "edit_cancel_did_not_send": "edit_cancel" in event_actions and db.get("messages", {}).get("cancel_message_count") == 0,
        "manual_send_ordinary_message": db.get("messages", {}).get("voice_bound_manual", 0) >= 1,
        "auto_send_ordinary_message": db.get("messages", {}).get("voice_bound_auto", 0) >= 1,
        "dangerous_confirmation_visible_before_reject": dangerous_confirmation_proof_valid(results) and "dangerous_confirmation_visible" in event_actions,
        "dangerous_rejected_without_execution": dangerous_confirmation_proof_valid(results) and "dangerous_confirmation_rejected" in event_actions,
        "tts_interrupt_before_recording": db.get("events", {}).get("tts_stopped", 0) >= 1 and "tts_interrupt" in event_actions,
        "tts_queue_settled": db.get("stop", {}).get("active_tts", 1) == 0,
        "global_stop_all_stages_observed": all(name in event_actions for name in GLOBAL_STOP_STAGE_EVENTS) and global_stop_proof_valid(results),
        "global_stop_terminal_state": global_stop_proof_valid(results),
        "global_stop_idempotent": global_stop_proof_valid(results),
        "global_stop_machine_audit_proof": global_stop_proof_valid(results),
        "half_duplex_order_verified": db.get("events", {}).get("half_duplex_order") is True,
        "no_uncontrolled_overlap_observed": "half_duplex" in event_actions,
        "physical_network_disconnected": network.get("offline", {}).get("physical_count", 0) >= 1 and network.get("offline", {}).get("up_count") == 0,
        "ollama_loopback_only": network.get("provider", {}).get("provider_id") == "ollama" and network.get("provider", {}).get("base_url") in {"http://127.0.0.1:11434", "http://localhost:11434"} and network.get("provider", {}).get("model") == "qwen3:4b",
        "no_external_owned_connections": network.get("connections", {}).get("public_connections") == [],
        "offline_voice_agent_tts_completed": db.get("offline", {}).get("successful_ollama_runs", 0) >= 1 and db.get("offline", {}).get("completed_tts", 0) >= 1,
        "network_state_restored": network.get("after") == network.get("before"),
        "formal_runtime_privacy_scan": all(value.get("marker_matches") == 0 for value in privacy.get("runtime_scans", {}).values()) if privacy.get("runtime_scans") else False,
        "diagnostic_bundle_privacy_scan": privacy.get("diagnostics", {}).get("entry_count", 0) >= 1 and privacy.get("diagnostics", {}).get("marker_matches") == 0,
        "diagnostic_exported_via_desktop_ui": "diagnostics_export" in event_actions,
        "voice_metadata_only_persistence": privacy.get("database", {}).get("all_forbidden_zero") is True and db.get("messages", {}).get("privacy_message_count", 0) >= 1,
        "temporary_audio_removed": privacy.get("temporary_audio_files_after", 1) == 0,
        "owned_processes_released": cleanup.get("owned_processes_released") is True,
        "test_owned_runtime_removed": cleanup.get("runtime_removed") is True,
        "external_processes_protected": cleanup.get("external_processes_protected") is True,
        "model_junction_removed": cleanup.get("model_junction_removed") is True,
        "graceful_candidate_shutdown": cleanup.get("forced") is False,
    }
    required = {name for names in REQUIRED_CHECKS.values() for name in names} | {"owned_processes_released", "test_owned_runtime_removed", "external_processes_protected", "model_junction_removed", "graceful_candidate_shutdown"}
    return {"checks": {name: {"passed": bool(checks.get(name))} for name in sorted(required)}, "status": "PASS" if all(checks.get(name) is True for name in required) else "FAIL"}


def _new_report(source: dict[str, Any], candidate: Path, runtime: Path, model: dict[str, Any]) -> dict[str, Any]:
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
        "input_contract": {"microphone": "REAL_WINDOWS_DEVICE_ONLY", "audio_file_upload": "FORBIDDEN", "synthetic_speech": "FORBIDDEN", "fixed_test_text_sensitive": False},
        "candidate": {"filename": candidate.name, "sha256": sha256_file(candidate), "identity_verified": False},
        "runtime": {"name": runtime.name, "isolated": True, "retained": False},
        "model": model,
        "operator_events": [],
        "limitations": ["Operator challenges attest visible and audible UI outcomes; transcript and recording contents are never stored in this report."],
    }


def _run_actions(
    report: dict[str, Any],
    privacy_marker: str,
    cancel_marker: str,
    harness: Any,
    desktop_pid: int,
    database: Path,
    runtime: Path,
) -> dict[str, Any]:
    observations: dict[str, Any] = {
        "devices": {"before": harness.audio_device_snapshot()},
        "network": {"before": network_snapshot()},
        "dangerous_confirmation": {},
        "global_stop": {},
    }
    for ordinal, spec in enumerate(ACTION_SPECS, 1):
        action = spec[0]
        audit_cursor = audit_log_watermark(database) if action in GLOBAL_STOP_STAGE_EVENTS else 0
        event_cursor = voice_event_watermark(database) if action in GLOBAL_STOP_STAGE_EVENTS else 0
        event = confirm_action(
            spec,
            ordinal,
            len(ACTION_SPECS),
            privacy_marker=privacy_marker,
            cancel_marker=cancel_marker,
            input_fn=harness.console_input,
        )
        report["operator_events"].append(event)
        if not event["valid"] or event["outcome"] != "PASS":
            raise DesktopAcceptanceError("operator confirmation failed, was stale, or did not match its challenge")
        if action == "device_disconnected":
            observations["devices"]["disconnected"] = harness.audio_device_snapshot()
        elif action == "device_restored":
            observations["devices"]["after"] = harness.audio_device_snapshot()
        elif action == "dangerous_confirmation_visible":
            observations["dangerous_confirmation"]["before_reject"] = dangerous_confirmation_observation(database)
        elif action == "dangerous_confirmation_rejected":
            observations["dangerous_confirmation"]["after_reject"] = dangerous_confirmation_observation(database)
        elif action in GLOBAL_STOP_STAGE_EVENTS:
            proof, _, _ = global_stop_stage_observation(
                database,
                runtime,
                action,
                audit_after_id=audit_cursor,
                event_after_id=event_cursor,
            )
            observations["global_stop"][action] = proof
        elif action == "offline_e2e":
            observations["network"]["offline"] = network_snapshot()
            observations["network"]["connections"] = owned_public_connections(harness, desktop_pid)
            observations["network"]["provider"] = provider_configuration_observation(runtime)
        elif action == "network_restored":
            observations["network"]["after"] = network_snapshot()
    return observations


def _prepare_runtime(harness: Any, candidate: Path, runtime: Path, model: Path, report: dict[str, Any]) -> tuple[Any, dict[str, Any], int, int, Path, dict[str, int], Path]:
    runtime.mkdir(parents=True, exist_ok=False)
    junction, model_summary = create_model_junction(runtime, model)
    report["model"] = model_summary
    process = harness._launch_candidate(candidate, runtime)
    desktop_identity = harness._identity_for_pid(process.pid)
    sidecar_pid, port = harness.find_sidecar(runtime, process.pid, process)
    health = harness.validate_candidate_health(harness.api_health(port), report["source"])
    report["candidate"].update({"identity_verified": True, "health": health})
    report["ownership"] = {"verified": True, "desktop_pid": process.pid, "sidecar_pid": sidecar_pid, "sidecar_parent_pid": process.pid, "api_port": port}
    database = runtime / "data" / "agent.db"
    harness._wait_for_database(database)
    return process, desktop_identity, sidecar_pid, port, database, database_watermark(database), junction


def _privacy_results(runtime: Path, database: Path, marker: str) -> dict[str, Any]:
    scans = {area: scan_files(runtime / area, marker) for area in ("logs", "crash", "temp")}
    temporary_audio = sum(1 for extension in ("*.wav", "*.mp3", "*.ogg", "*.webm") for item in (runtime / "temp").rglob(extension) if item.is_file())
    return {"runtime_scans": scans, "diagnostics": scan_diagnostics(database, marker), "database": privacy_database_observation(database, marker), "temporary_audio_files_after": temporary_audio}


def run_live_evidence(candidate: Path, model: Path) -> dict[str, Any]:
    harness = load_harness()
    source = harness.source_identity()
    runtime = EVIDENCE_ROOT / f"desktop-voice-runtime-{secrets.token_hex(8)}"
    report = _new_report(source, candidate, runtime, {})
    process = None
    desktop_identity = None
    junction: Path | None = None
    external_ollama_before: list[dict[str, Any]] = []
    privacy_marker = f"司忆隐私探针{secrets.randbelow(900000) + 100000}号"
    cancel_marker = f"SIYI_VOICE_CANCEL_{secrets.token_hex(8).upper()}"
    try:
        if os.name != "nt" or sys.platform != "win32" or not sys.stdin.isatty():
            raise DesktopAcceptanceError("formal desktop voice acceptance requires Windows and an interactive TTY")
        if source.get("source_version") != TARGET_VERSION or source.get("workspace_clean") is not True:
            raise DesktopAcceptanceError("formal desktop voice acceptance requires clean v14.0.0 source")
        external_ollama_before = harness.named_process_identities("ollama.exe")
        process, desktop_identity, _, _port, database, before, junction = _prepare_runtime(harness, candidate, runtime, model, report)
        observations = _run_actions(
            report, privacy_marker, cancel_marker, harness, process.pid, database, runtime
        )
        observations["database"] = collect_database_results(
            database, before, privacy_marker, cancel_marker
        )
        observations["privacy"] = _privacy_results(runtime, database, privacy_marker)
        report["results"] = observations
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "code": "DESKTOP_ACCEPTANCE_FAILED"}
    finally:
        stop = {"desktop_stopped": process is None, "forced": False}
        if process is not None and desktop_identity is not None:
            stop = harness.stop_owned_desktop(process, desktop_identity)
        junction_removed = remove_model_junction(junction)
        external_after = harness.named_process_identities("ollama.exe") if os.name == "nt" else []
        processes_released = process is None or process.poll() is not None
        removal = harness.cleanup_runtime(runtime) if processes_released and junction_removed and runtime.exists() else {"removed": not runtime.exists(), "reason": "owned cleanup precondition failed"}
        report["cleanup"] = {
            **stop,
            "model_junction_removed": junction_removed,
            "owned_processes_released": processes_released,
            "runtime_removed": removal.get("removed") is True,
            "external_processes_protected": external_after == external_ollama_before,
            "external_ollama_before": external_ollama_before,
            "external_ollama_after": external_after,
        }
        assessment = evaluate_report(report)
        report["checks"] = assessment["checks"]
        report["status"] = assessment["status"]
        report["finished_at"] = utc_now()
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect v14 formal desktop real-microphone acceptance evidence.")
    parser.add_argument("--candidate-exe", required=True, help="Formal packaged 司忆.exe candidate.")
    parser.add_argument("--stt-model-directory", required=True, help="Existing official small Faster-Whisper model directory.")
    parser.add_argument("--output", required=True, help="Fresh JSON path under build/v1400-evidence.")
    parser.add_argument("--execute", action="store_true", help="Explicitly launch the formal candidate and start operator acceptance.")
    arguments = parser.parse_args(argv)
    if not arguments.execute:
        parser.error("--execute is required; real microphone acceptance cannot be inferred or simulated")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        output = resolve_output(arguments.output)
        if output.exists():
            raise DesktopAcceptanceError(f"refusing to overwrite evidence: {output.name}")
        harness = load_harness()
        candidate = harness.resolve_candidate(arguments.candidate_exe)
        model = resolve_model_directory(arguments.stt_model_directory)
        report = run_live_evidence(candidate, model)
        write_json_immutable(output, report)
    except (DesktopAcceptanceError, OSError) as exc:
        print(f"v14 desktop voice acceptance failed before completion: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "output": output.relative_to(ROOT).as_posix()}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
