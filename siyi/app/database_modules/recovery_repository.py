from __future__ import annotations

import os
import time
import sqlite3


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)
        if handle:
            close_handle(handle)
            return True
        return ctypes.get_last_error() != 87
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError):
        return False
    return True


def _recover_orphaned_tasks(db: sqlite3.Connection) -> None:
    from app import database as facade
    from app.runtime.recovery import ensure_startup_recovery_checkpoint, reconcile_orphaned_operations
    from app.runtime.task_state import (
        ACTIVE_TASK_STATUS_VALUES,
        RESUMABLE_TASK_STATUS_VALUES,
        TERMINAL_TASK_STATUS_VALUES,
        TaskStatus,
    )
    now = time.time()
    for lease in db.execute("SELECT task_id,owner_pid,expires_at FROM task_leases WHERE status='active'").fetchall():
        if float(lease["expires_at"]) <= now or not _pid_is_alive(int(lease["owner_pid"])):
            db.execute(
                "UPDATE task_leases SET status='expired',released_at=?,expires_at=? WHERE task_id=? AND status='active'",
                (facade.now_iso(), now, lease["task_id"]),
            )
    stamp = facade.now_iso()
    active_placeholders = ",".join("?" for _ in ACTIVE_TASK_STATUS_VALUES)
    orphaned = db.execute(
        "SELECT id,status,current_step FROM agent_tasks "
        f"WHERE status IN ({active_placeholders}) AND NOT EXISTS ("
        "SELECT 1 FROM task_leases l WHERE l.task_id=agent_tasks.id "
        "AND l.status='active' AND l.expires_at>?) ORDER BY created_at,id",
        (*ACTIVE_TASK_STATUS_VALUES, now),
    ).fetchall()
    pending_ids = [str(row["id"]) for row in orphaned if str(row["status"]) == TaskStatus.PENDING.value]
    cancel_ids = [str(row["id"]) for row in orphaned if str(row["status"]) == TaskStatus.CANCEL_REQUESTED.value]
    interrupted_rows = [
        row
        for row in orphaned
        if str(row["status"]) not in {TaskStatus.PENDING.value, TaskStatus.CANCEL_REQUESTED.value}
    ]
    interrupted_ids = [str(row["id"]) for row in interrupted_rows]
    operation_recovery = reconcile_orphaned_operations(db, interrupted_ids)
    uncertain_ids = set(operation_recovery["uncertain_task_ids"])
    for row in interrupted_rows:
        task_id = str(row["id"])
        current_status = str(row["status"])
        checkpoint = ensure_startup_recovery_checkpoint(db, task_id)
        if checkpoint is None:
            target = TaskStatus.BLOCKED.value
            resumable = 0
            reason = "应用上次运行时异常终止，且缺少可信的副作用前检查点；已阻止自动重放"
        else:
            target = TaskStatus.INTERRUPTED.value
            resumable = 1
            reason = (
                "应用上次运行时异常终止，存在结果不确定的副作用；已阻止自动重放并等待明确恢复"
                if task_id in uncertain_ids
                else "应用上次运行时中断，可从最近检查点继续"
            )
        db.execute(
            "INSERT INTO task_transitions(task_id,from_status,to_status,reason,current_step,trigger_source,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (task_id, current_status, target, reason, row["current_step"], "database.startup_recovery", stamp),
        )
        db.execute(
            "UPDATE agent_tasks SET status=?,termination_reason=?,resumable=?,paused_at=?,updated_at=? WHERE id=? AND status=?",
            (target, reason, resumable, stamp, stamp, task_id, current_status),
        )
    for task_id in cancel_ids:
        reason = "应用重启时完成上次已请求的取消"
        db.execute(
            "INSERT INTO task_transitions(task_id,from_status,to_status,reason,current_step,trigger_source,created_at) "
            "SELECT id,status,?,?,current_step,'database.startup_recovery',? FROM agent_tasks WHERE id=? AND status=?",
            (TaskStatus.CANCELLED.value, reason, stamp, task_id, TaskStatus.CANCEL_REQUESTED.value),
        )
        db.execute(
            "UPDATE agent_tasks SET status=?,termination_reason=?,resumable=0,paused_at=NULL,"
            "finished_at=COALESCE(finished_at,?),updated_at=? WHERE id=? AND status=?",
            (TaskStatus.CANCELLED.value, reason, stamp, stamp, task_id, TaskStatus.CANCEL_REQUESTED.value),
        )
    if pending_ids:
        pending_placeholders = ",".join("?" for _ in pending_ids)
        db.execute(
            f"UPDATE agent_tasks SET resumable=1 WHERE id IN ({pending_placeholders})",
            tuple(pending_ids),
        )
    resumable_placeholders = ",".join("?" for _ in RESUMABLE_TASK_STATUS_VALUES)
    db.execute(
        f"UPDATE agent_tasks SET resumable=1 WHERE status IN ({resumable_placeholders})",
        RESUMABLE_TASK_STATUS_VALUES,
    )
    terminal_placeholders = ",".join("?" for _ in TERMINAL_TASK_STATUS_VALUES)
    db.execute(
        f"UPDATE agent_tasks SET resumable=0 WHERE status IN ({terminal_placeholders})",
        TERMINAL_TASK_STATUS_VALUES,
    )
    db.execute(
        "INSERT INTO task_transitions(task_id,from_status,to_status,reason,current_step,trigger_source,created_at) "
        "SELECT id,status,'interrupted','历史暂停任务已转换为可恢复中断',current_step,'database.startup_recovery',? "
        "FROM agent_tasks WHERE status='paused'",
        (stamp,),
    )
    db.execute(
        "UPDATE agent_tasks SET status='interrupted', termination_reason=COALESCE(termination_reason,'历史暂停任务已转换为可恢复中断'), "
        "resumable=1, updated_at=? WHERE status='paused'",
        (stamp,),
    )
