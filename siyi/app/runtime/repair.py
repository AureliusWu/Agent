from __future__ import annotations

import json
from typing import Any

from app.database import connect, now_iso
from app.runtime.task_leases import fence_current_task_write


MUTATION_TOOLS = {
    "create_file",
    "write_file",
    "replace_text",
    "apply_patch",
    "copy_file",
    "move_file",
    "rename_file",
    "create_directory",
    "delete_file",
    "undo_file_change",
    "undo_task_changes",
    "remember_workspace",
    "forget_workspace_memory",
    "create_worktree",
    "remove_worktree",
}
MUTATION_REPAIR_SCOPES = {"changes_recorded", "scope_control", "blocked_safely"}


def _scope_requires_mutation(scope: list[str]) -> bool:
    return any(item in MUTATION_REPAIR_SCOPES or item.startswith("path_state_") for item in scope)


def repair_tool_allowed(tool: str, retry_scope: list[str]) -> bool:
    return tool not in MUTATION_TOOLS or _scope_requires_mutation(retry_scope)


def build_repair_instruction(report: dict[str, Any], attempt: int, maximum: int) -> str:
    failed = [item.get("description") or item.get("criterion_id") for item in report.get("requirements_failed") or []]
    passed = [item.get("description") or item.get("criterion_id") for item in report.get("requirements_met") or []]
    failed_text = "\n".join(f"- {item}" for item in failed) or "- 没有可返工项"
    passed_text = "\n".join(f"- {item}" for item in passed) or "- 无"
    return (
        f"独立 Verifier 拒绝了本次完成声明。现在进行第 {attempt}/{maximum} 次限定返工。\n"
        f"只处理以下失败项：\n{failed_text}\n"
        f"以下项目已经通过，不得重复执行或扩大修改范围：\n{passed_text}\n"
        "先读取当前状态再返工，完成后运行必要验证。不要自行宣告 completed，最终状态仍由 Verifier 决定。"
    )


def start_repair(task_id: str, attempt: int, report: dict[str, Any]) -> None:
    scope = report.get("retry_scope") or []
    with connect() as db:
        fence_current_task_write(task_id, db=db)
        db.execute(
            "INSERT INTO task_repair_runs(task_id, attempt, status, retry_scope, before_fingerprint, reason, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                task_id,
                attempt,
                "running",
                json.dumps(scope, ensure_ascii=False),
                report.get("evidence_fingerprint") or "",
                report.get("reason") or "",
                now_iso(),
            ),
        )
        db.execute("UPDATE agent_tasks SET repair_attempts=?, updated_at=? WHERE id=?", (attempt, now_iso(), task_id))


def finish_repair(task_id: str, attempt: int, report: dict[str, Any]) -> bool:
    with connect() as db:
        fence_current_task_write(task_id, db=db)
        row = db.execute(
            "SELECT before_fingerprint FROM task_repair_runs WHERE task_id=? AND attempt=?",
            (task_id, attempt),
        ).fetchone()
        before = row[0] if row else ""
        after = report.get("evidence_fingerprint") or ""
        progressed = bool(after and after != before)
        db.execute(
            "UPDATE task_repair_runs SET status=?, after_fingerprint=?, reason=?, finished_at=? WHERE task_id=? AND attempt=?",
            ("passed" if report.get("status") == "passed" else ("progressed" if progressed else "no_progress"), after, report.get("reason") or "", now_iso(), task_id, attempt),
        )
    return progressed
