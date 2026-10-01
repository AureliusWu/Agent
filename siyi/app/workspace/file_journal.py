"""Metadata-only durable file steps. Observation never authorizes replay."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterator

from app.database import connect, now_iso
from app.config import settings
from app.workspace.file_recovery import (
    checked_path, file_state, RecoveryError, restore_plan, FORMAT_VERSION,
    _valid_state, _verify_backup, _overlay, state_token, content_state,
)


JOURNAL_VERSION = 1
TERMINAL_STATES = {"committed", "rolled_back", "preflight_failed"}
_active_step: ContextVar[tuple[str, int] | None] = ContextVar("file_journal_step", default=None)
_running: set[str] = set()
_running_lock = threading.Lock()


class JournalError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@contextmanager
def _process_mutex(operation_id: str) -> Iterator[None]:
    if os.name != "nt":
        # Product delivery is Windows. SQL claim still prevents duplicate
        # forward execution on other development platforms.
        yield
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = "Local\\SiyiFileTransaction-" + digest({"database": str(Path(settings.database_path).resolve()), "id": operation_id})
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise JournalError("operation_lock_unavailable", "无法建立跨进程文件事务锁；不会继续副作用")
    owned = False
    try:
        status = kernel.WaitForSingleObject(handle, 0)
        if status not in {0, 0x80}:  # acquired or abandoned by a dead process
            raise JournalError("operation_in_progress", "操作仍在其他进程执行；不会重复提交或核对运行中的现场")
        owned = True
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


@contextmanager
def executing(operation_id: str) -> Iterator[None]:
    validate_operation_id(operation_id)
    with _running_lock:
        if operation_id in _running:
            raise JournalError("operation_in_progress", "操作仍在执行；不会重复提交或核对运行中的现场")
        _running.add(operation_id)
    try:
        with _process_mutex(operation_id):
            yield
    finally:
        with _running_lock:
            _running.discard(operation_id)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def workspace_hash(workspace: str) -> str:
    return hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()


def validate_operation_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_:-]{15,127}", value):
        raise JournalError("invalid_operation_id", "operation_id 必须为 16 至 128 位稳定标识")
    return value


def existing(transaction_id: str, *, workspace: str, conversation_id: int | None,
             request_hash: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM file_transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    if (item["workspace_hash"] != workspace_hash(workspace) or item["conversation_id"] != conversation_id
            or item["request_hash"] != request_hash or item["journal_version"] != JOURNAL_VERSION):
        raise JournalError("operation_id_conflict", "操作标识已绑定不同计划、工作区或会话")
    if item["status"] == "preview":
        return None
    if item["status"] in TERMINAL_STATES:
        return {**json.loads(item["result_json"]), "batch_id": transaction_id,
                "operation_id": transaction_id, "replayed_receipt": True, "executed": False}
    return {"success": False, "status": "needs_attention", "batch_id": transaction_id,
            "operation_id": transaction_id, "error_code": "operation_reconciliation_required",
            "error_message": "此操作已开始；必须核对记录，不会自动重放文件副作用",
            "automatic_replay": False, "requires_new_authorization": True}


def prepare(transaction_id: str, *, workspace: str, conversation_id: int | None,
            request_hash: str, plan_hash: str, source: str, mode: str,
            steps: list[Any]) -> None:
    stamp = now_iso()
    with connect() as db:
        db.execute("UPDATE file_transactions SET conversation_id=?,request_hash=?,plan_hash=?,source=?,permission_mode=?,journal_version=? WHERE transaction_id=?",
                   (conversation_id, request_hash, plan_hash, source, mode, JOURNAL_VERSION, transaction_id))
        for index, step in enumerate(steps):
            db.execute("INSERT INTO file_transaction_steps(operation_id,transaction_id,step_index,operation,state,before_json,after_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(transaction_id,step_index) DO UPDATE SET before_json=excluded.before_json,after_json=excluded.after_json,updated_at=excluded.updated_at WHERE file_transaction_steps.state='planned'",
                       (f"{transaction_id}:{index}", transaction_id, index, step.operation, "planned",
                        json.dumps(step.before, sort_keys=True), json.dumps(step.after, sort_keys=True), stamp, stamp))
    checkpoint("plan_prepared", transaction_id, -1)


def checkpoint(phase: str, transaction_id: str, index: int) -> None:
    """Fault-injection seam. Production does not read environment crash switches."""


def step_state(transaction_id: str, index: int, state: str, *, change_id: str | None = None,
               result: dict[str, Any] | None = None) -> None:
    with connect() as db:
        cursor = db.execute("UPDATE file_transaction_steps SET state=?,change_id=COALESCE(?,change_id),result_json=?,updated_at=? WHERE transaction_id=? AND step_index=?",
                            (state, change_id, json.dumps(result or {}, sort_keys=True), now_iso(), transaction_id, index))
        if cursor.rowcount != 1:
            raise JournalError("journal_step_missing", "文件步骤日志缺失；不会继续副作用")
    checkpoint(state, transaction_id, index)


@contextmanager
def step_context(transaction_id: str, index: int) -> Iterator[None]:
    token = _active_step.set((transaction_id, index))
    try:
        yield
    finally:
        _active_step.reset(token)


def backup_verified(change_id: str) -> None:
    current = _active_step.get()
    if current is not None:
        step_state(*current, "backup_verified", change_id=change_id)
        step_state(*current, "effect_started", change_id=change_id)


def effect_observed(change_id: str) -> None:
    current = _active_step.get()
    if current is not None:
        checkpoint("after_manifest_before_journal", *current)
        step_state(*current, "effect_observed", change_id=change_id)


def _row(item: Any) -> dict[str, Any]:
    record = dict(item)
    if "transaction_id" in record and "operation_id" not in record:
        record["operation_id"] = record["transaction_id"]
    for key in ("plan_json", "result_json", "before_json", "after_json"):
        if key in record:
            record[key.removesuffix("_json")] = json.loads(record.pop(key))
    record.pop("workspace_hash", None)
    return record


def list_transactions(workspace: str, conversation_id: int, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
    with connect() as db:
        params = (workspace_hash(workspace), conversation_id)
        total = db.execute("SELECT COUNT(*) FROM file_transactions WHERE workspace_hash=? AND conversation_id=?", params).fetchone()[0]
        entries = db.execute("SELECT * FROM file_transactions WHERE workspace_hash=? AND conversation_id=? ORDER BY created_at DESC,transaction_id DESC LIMIT ? OFFSET ?",
                             (*params, limit, offset)).fetchall()
    return {"items": [_row(row) for row in entries], "total": total, "limit": limit, "offset": offset}


def detail(transaction_id: str, *, workspace: str, conversation_id: int) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM file_transactions WHERE transaction_id=? AND workspace_hash=? AND conversation_id=?",
                         (transaction_id, workspace_hash(workspace), conversation_id)).fetchone()
        if row is None:
            raise JournalError("transaction_not_found", "当前会话没有此文件事务")
        result = _row(row)
        result["steps"] = [_row(step) for step in db.execute("SELECT * FROM file_transaction_steps WHERE transaction_id=? ORDER BY step_index", (transaction_id,))]
    result.update(automatic_replay=False, requires_new_authorization=True)
    return result


def _manifest_for_step(root: Path, step: dict[str, Any]) -> dict[str, Any]:
    folder = checked_path(root, f".agent-backups/{step['change_id']}")
    record = checked_path(root, (folder / "manifest.json").relative_to(root).as_posix())
    if record.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("oversized manifest")
    manifest = json.loads(record.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION
            or manifest.get("id") != step["change_id"] or manifest.get("tool_call_id") != step["operation_id"]):
        raise ValueError("manifest ownership mismatch")
    entries = manifest.get("entries")
    if (not isinstance(entries, list) or not entries or not all(isinstance(entry, dict) for entry in entries)
            or len(entries) != len(step["before"]) or {entry.get("path") for entry in entries} != set(step["before"])):
        raise ValueError("manifest plan paths mismatch")
    for entry in entries:
        checked_path(root, entry["path"])
        if not _valid_state(entry.get("before")) or state_token(entry["before"]) != step["before"][entry["path"]]:
            raise ValueError("manifest precondition mismatch")
        _verify_backup(root, folder, entry)
        if entry.get("after") is not None:
            expected = {**step["before"], **step["after"]}[entry["path"]]
            after = entry["after"]
            if (not _valid_state(after) or (expected == "directory:planned" and after.get("type") != "directory")
                    or (expected != "directory:planned" and state_token(after) != expected)):
                raise ValueError("manifest postcondition mismatch")
    return manifest


def reconcile(transaction_id: str, *, workspace: str, conversation_id: int) -> dict[str, Any]:
    with executing(transaction_id):
        return _reconcile(transaction_id, workspace=workspace, conversation_id=conversation_id)


def _reconcile(transaction_id: str, *, workspace: str, conversation_id: int) -> dict[str, Any]:
    item = detail(transaction_id, workspace=workspace, conversation_id=conversation_id)
    if item["journal_version"] != JOURNAL_VERSION:
        raise JournalError("journal_legacy_uncertain", "历史记录缺少步骤证据；只能人工核对")
    root = Path(workspace).resolve(strict=True)
    observations: list[dict[str, Any]] = []
    events: list[tuple[int, dict[str, Any], dict[str, Any], bool]] = []
    for step in item["steps"]:
        state = step["state"]
        observed = "needs_attention"
        reason = "missing_effect_evidence"
        try:
            change_id = step["change_id"]
            if state == "planned" and not change_id:
                observed, reason = "not_started", "no_step_started"
            elif change_id:
                manifest = _manifest_for_step(root, step)
                entries = manifest["entries"]
                recovery = manifest.get("recovery") or {}
                if not isinstance(recovery, dict):
                    raise ValueError("invalid recovery record")
                if recovery.get("status") == "restored":
                    states = recovery.get("after")
                    if (not isinstance(states, dict) or set(states) != set(step["before"])
                            or not all(_valid_state(value) for value in states.values())
                            or not isinstance(recovery.get("completed_at_ns"), int)):
                        raise ValueError("missing restored state proof")
                    if any(content_state(states[entry["path"]]) != content_state(entry["before"]) for entry in entries):
                        raise ValueError("restored state is not the backup state")
                    events.append((recovery["completed_at_ns"], step, states, True))
                elif all(entry.get("after") for entry in entries):
                    events.append((int(change_id.split("-", 1)[0]), step,
                                   {entry["path"]: entry["after"] for entry in entries}, False))
                elif state == "backup_verified" and all(file_state(checked_path(root, entry["path"])) == entry["before"] for entry in entries):
                    observed, reason = "not_started", "backup_verified_before_effect"
            elif state == "committed" and not step["after"]:
                observed, reason = "completed", "read_only_receipt"
        except (OSError, ValueError, KeyError, TypeError):
            observed, reason = "needs_attention", "invalid_or_missing_manifest"
        observations.append({"operation_id": step["operation_id"], "step_index": step["step_index"],
                             "state": state, "observed_state": observed, "reason": reason, "change_id": step["change_id"]})
    by_index = {entry["step_index"]: entry for entry in observations}
    latest: dict[str, dict[str, Any]] = {}
    # Forward effects commit in plan order; compensation commits in reverse.
    # Use durable event order and validate the newest state before accepting an
    # older superseded receipt. A restored label alone is never disk evidence.
    for _, step, states, compensated in sorted(events, key=lambda event: event[0], reverse=True):
        try:
            valid = True
            for path, expected in states.items():
                if path in latest and (compensated or step["state"] == "committed"):
                    continue
                combined = _overlay(expected, path, latest)
                if file_state(checked_path(root, path)) != combined:
                    valid = False
                    break
            observation = by_index[step["step_index"]]
            observation.update(observed_state=("compensated" if compensated else "completed") if valid else "needs_attention",
                               reason=("restored_after_matches" if compensated else "durable_after_matches") if valid else "workspace_drift")
            if valid:
                for path, expected in states.items():
                    latest.setdefault(path, expected)
        except (OSError, ValueError, KeyError, TypeError):
            by_index[step["step_index"]].update(observed_state="needs_attention", reason="invalid_or_missing_manifest")
    status = "reconciled" if all(row["observed_state"] in {"completed", "compensated", "not_started"} for row in observations) else "needs_attention"
    # Observation is metadata only. No grants, workspace writes or resume token.
    with connect() as db:
        if item["status"] not in TERMINAL_STATES | {"preview", "restored"}:
            db.execute("UPDATE file_transactions SET status=?,updated_at=? WHERE transaction_id=?", (status, now_iso(), transaction_id))
    return {"operation_id": transaction_id, "status": status, "steps": observations,
            "automatic_replay": False, "requires_new_authorization": True, "workspace_modified": False}


def restore_batch(transaction_id: str, *, workspace: str, conversation_id: int, authority_check=None) -> dict[str, Any]:
    with executing(transaction_id):
        return _restore_batch(transaction_id, workspace=workspace, conversation_id=conversation_id, authority_check=authority_check)


def _restore_batch(transaction_id: str, *, workspace: str, conversation_id: int, authority_check=None) -> dict[str, Any]:
    """Called only behind fresh Tool permission and workspace recovery ownership."""
    item = detail(transaction_id, workspace=workspace, conversation_id=conversation_id)
    if item["status"] != "committed" or not item["steps"] or any(step["state"] != "committed" for step in item["steps"]):
        raise RecoveryError("recovery_batch_not_complete", "批次不是完整提交状态；先核对中断记录，不能强制整批恢复")
    root = Path(workspace).resolve(strict=True)
    folders: list[Path] = []
    for step in reversed(item["steps"]):
        if not step["change_id"]:
            continue
        folder = checked_path(root, f".agent-backups/{step['change_id']}")
        try:
            _manifest_for_step(root, step)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryError("recovery_evidence_missing", "恢复记录不属于本批步骤或已损坏") from exc
        folders.append(folder)
    if not folders:
        raise RecoveryError("recovery_no_changes", "此批没有可恢复的文件副作用")
    try:
        changes = restore_plan(root, folders, authority_check=authority_check)
    except RecoveryError:
        # Keep committed as historical outcome; F01 manifests own partial
        # recovery status, so a second attempt is rejected by their proof.
        with connect() as db:
            db.execute("UPDATE file_transactions SET status='needs_attention',updated_at=? WHERE transaction_id=?", (now_iso(), transaction_id))
        raise
    result = {"operation_id": transaction_id, "undone": len(changes), "changes": changes,
              "restored": [path for change in changes for path in change["restored"]], "backup_retained": True}
    with connect() as db:
        db.execute("UPDATE file_transactions SET status='restored',updated_at=? WHERE transaction_id=?", (now_iso(), transaction_id))
        db.execute("UPDATE file_transaction_steps SET state='compensated',updated_at=? WHERE transaction_id=? AND change_id IS NOT NULL", (now_iso(), transaction_id))
    return result
