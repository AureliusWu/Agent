from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import uuid
from typing import Any

from .database import connect, now_iso


_INSTANCE_ID = uuid.uuid4().hex
_ACTIVE: dict[int, str] = {}
_HANDLES: dict[int, Any] = {}


def _process_identity(pid: int) -> str | None:
    """Return a creation fingerprint so a recycled PID is never terminated."""
    if pid <= 0:
        return None
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            if exit_code.value != 259:  # STILL_ACTIVE
                return None
            creation = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            ok = ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            return f"win:{creation.value}" if ok else None
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="utf-8").split()
        return f"proc:{fields[21]}" if len(fields) > 21 else None
    except (OSError, UnicodeError):
        return None


def register_process(pid: int, task_id: str | None, command: str, args: list[str], process: Any | None = None) -> None:
    if process is not None:
        _HANDLES[pid] = process
    if not task_id:
        return
    identity = _process_identity(pid)
    if identity is None:
        raise RuntimeError("无法读取子进程创建指纹")
    digest = hashlib.sha256("\0".join([command, *args]).encode("utf-8")).hexdigest()
    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO managed_processes("
            "pid,task_id,owner_pid,owner_instance_id,process_identity,command_hash,started_at,status,stopped_at"
            ") VALUES(?,?,?,?,?,?,?,'running',NULL)",
            (pid, task_id, os.getpid(), _INSTANCE_ID, identity, digest, now_iso()),
        )
    _ACTIVE[pid] = task_id


def unregister_process(pid: int, status: str = "exited") -> None:
    _ACTIVE.pop(pid, None)
    _HANDLES.pop(pid, None)
    with connect() as db:
        db.execute(
            "UPDATE managed_processes SET status=?,stopped_at=? WHERE pid=? AND status='running'",
            (status, now_iso(), pid),
        )


def terminate_process_tree(pid: int, expected_identity: str | None = None) -> bool:
    if expected_identity is not None and _process_identity(pid) != expected_identity:
        return False
    if _process_identity(pid) is None:
        return True
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return True
        handle = _HANDLES.get(pid)
        if handle is not None:
            try:
                handle.kill()
                return True
            except (OSError, ProcessLookupError):
                return _process_identity(pid) is None
        return _process_identity(pid) is None
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return _process_identity(pid) is None


def terminate_task_processes(task_id: str, reason: str = "cancelled") -> int:
    with connect() as db:
        records = db.execute(
            "SELECT pid,process_identity FROM managed_processes WHERE task_id=? AND status='running'",
            (task_id,),
        ).fetchall()
    stopped = 0
    for record in records:
        pid = int(record["pid"])
        if terminate_process_tree(pid, str(record["process_identity"])):
            stopped += 1
            unregister_process(pid, reason)
    return stopped


def terminate_all_processes(reason: str = "shutdown") -> int:
    with connect() as db:
        task_ids = [str(row[0]) for row in db.execute(
            "SELECT DISTINCT task_id FROM managed_processes WHERE status='running' AND owner_instance_id=?",
            (_INSTANCE_ID,),
        ).fetchall()]
    return sum(terminate_task_processes(task_id, reason) for task_id in task_ids)


def recover_orphaned_processes() -> int:
    """Reclaim only stale children whose PID creation fingerprint still matches."""
    with connect() as db:
        records = db.execute(
            "SELECT pid,process_identity FROM managed_processes WHERE status='running' AND owner_instance_id<>?",
            (_INSTANCE_ID,),
        ).fetchall()
    recovered = 0
    for record in records:
        pid = int(record["pid"])
        identity = str(record["process_identity"])
        if _process_identity(pid) is None:
            unregister_process(pid, "orphan_missing")
        elif terminate_process_tree(pid, identity):
            recovered += 1
            unregister_process(pid, "orphan_recovered")
        else:
            with connect() as db:
                db.execute(
                    "UPDATE managed_processes SET status='recovery_failed',stopped_at=? WHERE pid=? AND status='running'",
                    (now_iso(), pid),
                )
    return recovered
