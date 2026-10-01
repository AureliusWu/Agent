"""Opt-in qualification through the real Runtime/Executor and isolated disk.

No imports or reads here cause network calls. The caller must own the complete
data/config/workspace root. Scripted controls prove the kernel, never a model.
Report validation establishes the protocol, not artifact provenance/signatures;
the RC aggregator must additionally bind source/build identity and file hashes.
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app import __version__
from app.providers.schema_validation import prepare_schema, parse_output, validate_output

from .runtime_cases import CASES, LEVEL_CASES, REQUIRED_CASES, FileCase, scripted_completion

PROTOCOL = "runtime-file-v1"
TARGET_VERSION = "16.0.0"
MIN_SAMPLES = 3
IDENTITY_FIELDS = ("provider_id", "endpoint_hash", "model", "model_digest", "configuration_hash", "identity_hash")
ALLOWED_TOOLS = frozenset({"read_file", "read_file_range", "list_files", "list_directory", "search_files", "search_text", "file_metadata", "file_info", "file_diff", "view_diff", "compare_files", "create_file", "write_file", "replace_text", "apply_patch", "rename_file", "move_file", "create_directory", "list_file_changes", "undo_file_change"})
PLAN_SCHEMA = {"type": "object", "required": ["steps"], "additionalProperties": False, "properties": {"steps": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "string"}}}}


def _filesystem_type(root: Path) -> str:
    if os.name != "nt":
        return "unknown"
    volume, filesystem = ctypes.create_unicode_buffer(260), ctypes.create_unicode_buffer(260)
    try:
        kernel = ctypes.windll.kernel32
        if not kernel.GetVolumePathNameW(str(root), volume, 260):
            return "unknown"
        if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, filesystem, 260):
            return "unknown"
        return filesystem.value.upper()
    except (AttributeError, OSError):
        return "unknown"


def _isolation(root: Path) -> None:
    from app.config import settings
    from app.providers.configuration import provider_config_path
    marker = root / ".runtime-qualification-owned"
    data_root = os.getenv("AGENT_DATA_ROOT")
    paths = (settings.database_path, settings.log_path, provider_config_path(), Path(data_root or root),
             settings.extension_directory or root / "extensions")
    if (not marker.is_file() or marker.read_text(encoding="utf-8") != PROTOCOL
            or not data_root or root == Path(root.anchor) or any(not path.resolve().is_relative_to(root) for path in paths)):
        raise ValueError("Runtime qualification requires an isolated test-owned database/config/data root")


class _ScopedTools:
    """Only narrow the benchmark surface; permitted calls keep the real kernel."""
    def __init__(self, delegate):
        self.delegate = delegate

    async def execute(self, **kwargs):
        if kwargs["name"] not in ALLOWED_TOOLS:
            raise ValueError("Qualification tool scope exceeded")
        return await self.delegate.execute(**kwargs)


def _disk_snapshot(workspace: Path) -> dict[str, str]:
    result = {}
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace)
        if relative.parts[0] in {".agent", ".agent-backups"}:
            continue
        if path.is_symlink():
            result[relative.as_posix()] = "unexpected_symlink"
        elif path.is_file():
            if path.stat().st_size > 1024 * 1024 or len(result) >= 100:
                raise ValueError("Qualification filesystem bounds exceeded")
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _hash_files(files: dict[str, str]) -> dict[str, str]:
    return {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}


def _receipts(task_id: str) -> list[dict[str, Any]]:
    from app.database import rows
    safe = []
    for row in rows("SELECT receipt_json FROM tool_receipts WHERE task_id=? ORDER BY created_at,receipt_id", (task_id,)):
        receipt = json.loads(row["receipt_json"])
        # Omit content, normalized arguments, full paths and exception text.
        safe.append({key: receipt.get(key) for key in (
            "receipt_id", "task_id", "tool_call_id", "tool", "operation_kind", "success", "standard_status",
            "change_id", "error_code", "permission_decision", "risk_level",
        )})
    return safe


def _receipt_passed(case: FileCase, receipts: list[dict]) -> bool:
    # The import reader must enforce the same narrowed tool surface as the
    # running harness. Extra approved shell/MCP receipts are not evidence of
    # this isolated file contract, even when the final disk hashes match.
    if any(item.get("tool") not in ALLOWED_TOOLS for item in receipts):
        return False
    if not case.actions:
        return not receipts
    if case.control:
        expected_error = "read_only_mode" if case.case_id == "kernel-readonly-deny" else "tool_error"
        return len(receipts) == 1 and any(item.get("success") is False and item.get("tool") == case.actions[0][0]
                   and item.get("permission_decision") == "evaluated" and item.get("error_code") == expected_error for item in receipts)
    for name, _ in case.actions:
        if not any(item.get("tool") == name and item.get("success") is True
                   and item.get("permission_decision") == "approved"
                   and (item.get("operation_kind") != "mutation" or item.get("change_id")) for item in receipts):
            return False
    return all(item.get("success") is True and item.get("permission_decision") == "approved"
               and (item.get("operation_kind") != "mutation" or item.get("change_id")) for item in receipts)


async def _dispatch_runtime(*args, **kwargs):
    from app.runtime.runner import run_chat
    return await run_chat(*args, **kwargs)


async def _refresh_live_identity(*, expected: dict | None, timeout: float) -> tuple[dict, list[str]]:
    """Renew only read-only model metadata; never replace the bound identity."""
    from app.providers.configuration import load_provider_configuration
    from app.providers.effective_capabilities import resolve_effective_capabilities
    from app.providers.ollama import OllamaProvider
    config = load_provider_configuration()
    before = resolve_effective_capabilities(configuration=config).public()
    # TTL expiry temporarily removes observations; configuration drift does
    # not. Do not query an old model after the selected configuration changes.
    if expected is not None and before.get("configuration_hash") != expected.get("configuration_hash"):
        return before, ["identity_mismatch:configuration_hash"]
    if config.provider_id != "ollama":
        return before, ["local_provider_required"]
    try:
        metadata = await asyncio.wait_for(OllamaProvider(config).diagnostics(), timeout=min(timeout, 45.0))
        if not isinstance(metadata, dict) or metadata.get("status") != "ok":
            return before, ["metadata_refresh_failed"]
    except Exception:
        return before, ["metadata_refresh_failed"]
    current = resolve_effective_capabilities(configuration=load_provider_configuration()).public()
    return current, _identity_errors(expected or current, current)


async def _run_sample(case: FileCase, sample: int, root: Path, *, mode: str, completion_factory: Callable | None, timeout: float) -> dict[str, Any]:
    from app.database import connect, now_iso
    from app.kernel.services import build_kernel_services
    from app.runtime.runner import TaskLimits
    from app.schemas import ChatRequest
    owned = root / f"{case.case_id}-{sample}-{uuid.uuid4().hex}"
    workspace = owned / "workspace"
    workspace.mkdir(parents=True)
    sentinel = owned / "outside.txt"
    sentinel.write_text("outside-sentinel", encoding="utf-8")
    for name, content in case.initial.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    if case.case_id == "file-move":
        (workspace / "archive").mkdir()
    with connect() as db:
        cursor = db.execute("INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                            ("Isolated qualification", str(workspace), case.mode, now_iso(), now_iso()))
        conversation_id = cursor.lastrowid
    task_id = uuid.uuid4().hex
    scripted = mode == "scripted" or case.control
    complete = (completion_factory or scripted_completion)(case) if scripted else None
    services = build_kernel_services(complete)
    services = replace(services, tools=_ScopedTools(services.tools))
    started = time.perf_counter()
    result, error = {}, None
    try:
        result = await asyncio.wait_for(_dispatch_runtime(
            ChatRequest(conversation_id=conversation_id, task_id=task_id, content=case.prompt,
                        memory_write_policy="deny", token_budget_limit=120_000,
                        segment_timeout_seconds=timeout, preferred_model=None),
            kernel_services=services,
            limits=replace(TaskLimits.current(), max_agent_rounds=8, max_tool_calls=16,
                           task_timeout_seconds=timeout, max_repair_attempts=0),
        ), timeout=timeout + 5)
    except Exception as exc:
        error = type(exc).__name__
    receipts = _receipts(task_id)
    disk = _disk_snapshot(workspace)
    disk_ok = disk == _hash_files(case.expected)
    sentinel_ok = sentinel.read_text(encoding="utf-8") == "outside-sentinel"
    response_ok = bool(result.get("content"))
    if case.case_id in {"basic-chat", "readonly-read"}:
        response_ok = str(result.get("content") or "").strip() == ("4" if case.case_id == "basic-chat" else "qualified-read")
    if case.case_id == "structured-plan":
        try:
            plan = parse_output(str(result.get("content") or ""))
            validate_output(plan, prepare_schema(PLAN_SCHEMA))
            response_ok = plan == {"steps": ["inspect", "edit", "verify"]}
        except Exception:
            response_ok = False
    with connect() as db:
        model_rows = list(db.execute("SELECT provider,model,success FROM model_runs WHERE task_id=?", (task_id,)))
        operations = list(db.execute("SELECT status FROM task_operations WHERE task_id=?", (task_id,)))
        audits = db.execute("SELECT COUNT(*) FROM audit_logs WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
    status = result.get("task_status", "unknown")
    terminal_ok = status == "completed" if not case.control else status in {"completed", "partially_completed", "failed"}
    receipt_ok = _receipt_passed(case, receipts)
    evidence = {
        "executor": type(services.executor).__name__, "runtime": "app.runtime.runner.run_chat",
        "permission_mode": case.mode, "task_id": task_id, "task_status": status,
        "filesystem_passed": disk_ok, "outside_sentinel_unchanged": sentinel_ok,
        "observed_files_sha256": disk, "expected_files_sha256": _hash_files(case.expected),
        "receipts": receipts, "receipt_passed": receipt_ok, "response_passed": response_ok,
        "operation_statuses": [row["status"] for row in operations], "audit_count": audits,
        "successful_model_requests": sum(row["success"] == 1 for row in model_rows),
        "model_identities": sorted({f"{row['provider']}:{row['model']}" for row in model_rows}),
        "verification": {"status": (result.get("verification") or {}).get("status"),
                         "criteria": [{key: item.get(key) for key in ("criterion_id", "kind", "status")}
                                      for item in (result.get("verification") or {}).get("checks", []) if isinstance(item, dict)]},
    }
    passed = error is None and terminal_ok and disk_ok and sentinel_ok and response_ok and receipt_ok
    return {"case_id": case.case_id, "requirement_id": f"V160-RUNTIME-FILE-V1-{case.case_id.upper()}",
            "sample": sample, "origin": "scripted_control" if case.control else ("scripted_model" if scripted else "local_live"),
            "status": "passed" if passed else "failed", "error_type": error,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3), "evidence": evidence}


def _identity_errors(snapshot: Any, current: Any) -> list[str]:
    if not isinstance(snapshot, dict) or not isinstance(current, dict):
        return ["identity_missing"]
    errors = []
    for name in IDENTITY_FIELDS:
        if not snapshot.get(name) or snapshot.get(name) != current.get(name):
            errors.append("identity_mismatch:" + name)
    if snapshot.get("provider_id") != "ollama" or snapshot.get("local") is not True:
        errors.append("local_provider_required")
    for key in ("model_digest", "configuration_hash", "identity_hash"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(snapshot.get(key) or "")):
            errors.append("invalid_identity:" + key)
    if snapshot.get("observation_stale") is not False or current.get("observation_stale") is not False:
        errors.append("stale_observation")
    if type(snapshot.get("context_window_tokens")) is not int or snapshot["context_window_tokens"] <= 0:
        errors.append("unknown_window")
    return errors


def validate_qualification_report(payload: Any, *, current_identity: dict, target_version: str = TARGET_VERSION, level: str = "file_agent") -> list[str]:
    """Return stable error codes. Empty means this protocol/level is eligible.

    Does not trust stored pass rates/qualified flags. Legacy simulation, missing
    repetitions, duplicate samples, unknown receipts and identity drift fail.
    """
    try:
        return _validate_qualification_report(payload, current_identity=current_identity, target_version=target_version, level=level)
    except (TypeError, ValueError, KeyError, OverflowError):
        return ["malformed_report"]


def _validate_qualification_report(payload: Any, *, current_identity: dict, target_version: str, level: str) -> list[str]:
    if not isinstance(payload, dict) or level not in LEVEL_CASES or target_version != TARGET_VERSION:
        return ["invalid_report"]
    errors = []
    if payload.get("schema_version") != 1 or type(payload.get("schema_version")) is not int:
        errors.append("invalid_schema_version")
    if payload.get("preflight_errors") != []:
        errors.append("preflight_not_passed")
    if level == "file_agent" and payload.get("status") != "passed":
        errors.append("run_not_passed")
    for key, expected in {"target_version": target_version, "protocol": PROTOCOL, "evidence_layer": "runtime_filesystem", "mode": "local_live"}.items():
        if payload.get(key) != expected:
            errors.append("invalid_" + key)
    if payload.get("filesystem") != "NTFS":
        errors.append("ntfs_not_verified")
    errors.extend(_identity_errors(payload.get("effective_capabilities"), current_identity))
    if payload.get("identity_stable") is not True:
        errors.append("identity_changed")
    results = payload.get("case_results")
    if not isinstance(results, list) or len(results) > 100:
        return [*errors, "invalid_case_results"]
    if any(not isinstance(row, dict) for row in results):
        return [*errors, "invalid_case_result"]
    if {row.get("case_id") for row in results} != set(REQUIRED_CASES):
        errors.append("case_manifest_mismatch")
    snapshot = payload.get("effective_capabilities") or {}
    task_ids: set[str] = set()
    receipt_ids: set[str] = set()
    for case in CASES:
        if case.case_id not in LEVEL_CASES[level]:
            continue
        matches = [row for row in results if row.get("case_id") == case.case_id]
        samples = [row.get("sample") for row in matches]
        if len(matches) < MIN_SAMPLES or any(type(value) is not int or value < 1 for value in samples) or len(set(map(str, samples))) != len(samples) or sorted(samples) != list(range(1, len(samples) + 1)):
            errors.append("insufficient_or_duplicate_samples:" + case.case_id)
        for row in matches:
            evidence = row.get("evidence")
            expected_origin = "scripted_control" if case.control else "local_live"
            if (row.get("status") != "passed" or row.get("error_type") is not None or row.get("origin") != expected_origin
                    or row.get("requirement_id") != f"V160-RUNTIME-FILE-V1-{case.case_id.upper()}"):
                errors.append("case_not_passed:" + case.case_id)
            if not isinstance(evidence, dict):
                errors.append("missing_evidence:" + case.case_id)
                continue
            task_id = evidence.get("task_id")
            if not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{32}", task_id) or task_id in task_ids:
                errors.append("missing_or_reused_task:" + case.case_id)
            else:
                task_ids.add(task_id)
            receipts = evidence.get("receipts")
            valid_receipts = isinstance(receipts, list) and all(isinstance(item, dict) and item.get("receipt_id") and item.get("task_id") == evidence.get("task_id") for item in receipts)
            if valid_receipts:
                for receipt in receipts:
                    receipt_id = receipt["receipt_id"]
                    if not isinstance(receipt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", receipt_id) or receipt_id in receipt_ids:
                        valid_receipts = False
                    else:
                        receipt_ids.add(receipt_id)
            terminal_ok = evidence.get("task_status") == "completed" if not case.control else evidence.get("task_status") in {"completed", "partially_completed", "failed"}
            if (evidence.get("runtime") != "app.runtime.runner.run_chat" or evidence.get("executor") != "LocalWindowsExecutor"
                    or evidence.get("permission_mode") != case.mode or not terminal_ok
                    or evidence.get("filesystem_passed") is not True or evidence.get("outside_sentinel_unchanged") is not True
                    or evidence.get("response_passed") is not True or not valid_receipts
                    or (valid_receipts and not _receipt_passed(case, receipts))
                    or evidence.get("observed_files_sha256") != _hash_files(case.expected)
                    or evidence.get("expected_files_sha256") != _hash_files(case.expected)):
                errors.append("invalid_runtime_evidence:" + case.case_id)
            if case.actions and (type(evidence.get("audit_count")) is not int or evidence["audit_count"] < 1):
                errors.append("audit_missing:" + case.case_id)
            if not case.control and any(item.get("operation_kind") == "mutation" for item in receipts or []) and (
                not isinstance(evidence.get("operation_statuses"), list) or not evidence["operation_statuses"]
                or any(status != "completed" for status in evidence["operation_statuses"])
            ):
                errors.append("operation_not_completed:" + case.case_id)
            if not case.control and (type(evidence.get("successful_model_requests")) is not int or evidence["successful_model_requests"] < 1
                    or evidence.get("model_identities") != [f"{snapshot.get('provider_id')}:{snapshot.get('model')}"]):
                errors.append("live_model_evidence_missing:" + case.case_id)
    return list(dict.fromkeys(errors))


async def run_runtime_file_qualification(*, isolation_root: str | Path, mode: str = "scripted", samples: int = 3, timeout_seconds: float = 180, completion_factory: Callable | None = None) -> dict[str, Any]:
    from app.providers.configuration import load_provider_configuration
    from app.providers.effective_capabilities import resolve_effective_capabilities
    if mode not in {"scripted", "local_live"} or type(samples) is not int or not 1 <= samples <= 10 or not 0 < timeout_seconds <= 600:
        raise ValueError("Invalid qualification mode, sample count or timeout")
    if mode == "local_live" and (completion_factory is not None or os.getenv("SIYI_TEST_PROVIDER") != "ollama"):
        raise ValueError("local_live requires explicit SIYI_TEST_PROVIDER=ollama and forbids injected completion")
    root = Path(isolation_root).resolve()
    _isolation(root)
    config = load_provider_configuration()
    if config.provider_id != ("mock" if mode == "scripted" else "ollama"):
        raise ValueError("Qualification provider does not match mode")
    if mode == "local_live":
        snapshot, preflight_errors = await _refresh_live_identity(expected=None, timeout=timeout_seconds)
    else:
        snapshot, preflight_errors = resolve_effective_capabilities(configuration=config).public(), []
    report = {"schema_version": 1, "target_version": TARGET_VERSION, "app_version": __version__, "protocol": PROTOCOL,
              "mode": mode, "evidence_layer": "runtime_filesystem", "run_id": uuid.uuid4().hex,
              "started_at": datetime.now(timezone.utc).isoformat(), "filesystem": _filesystem_type(root),
              "effective_capabilities": snapshot, "identity_stable": True, "case_results": [], "preflight_errors": preflight_errors,
              "limitations": ["Scripted kernel controls are not live-model capability evidence.", "Fixed prompted-tool small-file cases do not qualify delete/copy, large-file, crash recovery, batch transactions or performance.", "Structured plan is a fixed bounded JSON contract, not general planning quality.", "RC must bind report source/build provenance and artifact hashes independently."]}
    started = time.perf_counter()
    if not preflight_errors:
        for case in CASES:
            for sample in range(1, samples + 1):
                if mode == "local_live":
                    _, refresh_errors = await _refresh_live_identity(expected=snapshot, timeout=timeout_seconds)
                    if refresh_errors:
                        preflight_errors.extend(refresh_errors)
                        break
                report["case_results"].append(await _run_sample(case, sample, root, mode=mode, completion_factory=completion_factory, timeout=timeout_seconds))
            if preflight_errors:
                break
    final = resolve_effective_capabilities(configuration=load_provider_configuration()).public()
    if mode == "local_live" and not preflight_errors:
        final, refresh_errors = await _refresh_live_identity(expected=snapshot, timeout=timeout_seconds)
        preflight_errors.extend(refresh_errors)
    report["identity_stable"] = all(snapshot.get(name) == final.get(name) for name in IDENTITY_FIELDS)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    results = report["case_results"]
    report["metrics"] = {"samples": len(results), "passed_samples": sum(row["status"] == "passed" for row in results), "failed_samples": sum(row["status"] != "passed" for row in results), "duration_ms": round((time.perf_counter() - started) * 1000, 3)}
    report["status"] = "blocked" if preflight_errors else ("passed" if all(row["status"] == "passed" for row in results) else "failed")
    report["qualification"] = {level: {"qualified": not (errors := validate_qualification_report(report, current_identity=final, level=level)), "errors": errors} for level in LEVEL_CASES}
    report["file_agent_qualified"] = report["qualification"]["file_agent"]["qualified"]
    return report
