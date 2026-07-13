import json

from fastapi import APIRouter, Header, HTTPException

from .. import __version__
from ..config import settings
from ..database import audit, backup_database, database_backups, database_status, restore_database, rows
from ..environment import detect_build_environment
from ..model_routing import routing_policy
from ..provider import provider_health

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health() -> dict:
    db = database_status()
    return {"status": "ok" if db["status"] == "ok" else "error", "version": __version__, "database": db, "model": settings.model_name}


@router.get("/provider/health")
async def model_health(x_model_api_key: str | None = Header(default=None)) -> dict:
    return await provider_health(x_model_api_key)


@router.get("/provider/policy")
def model_policy() -> dict:
    return {
        **routing_policy(),
        "budgets": {
            "task_tokens": settings.max_task_tokens,
            "phase_tokens": settings.max_phase_tokens,
            "model_call_tokens": settings.max_model_call_tokens,
            "tool_result_chars": settings.max_tool_result_chars,
            "file_snippet_chars": settings.max_file_snippet_chars,
            "skill_chars": settings.max_skill_context_chars,
            "memory_items": settings.max_memory_items,
        },
        "cache": {
            "read_tool_ttl_seconds": settings.read_cache_ttl_seconds,
            "skill_content": True,
            "project_signature": True,
            "build_environment": True,
            "tool_definitions": True,
        },
    }


@router.get("/workspaces/environment")
def workspace_environment(workspace: str) -> dict:
    try:
        return detect_build_environment(workspace)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/audit")
def audit_logs(limit: int = 100) -> list[dict]:
    return rows("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),))


@router.get("/tasks/recent")
def recent_tasks(limit: int = 30) -> list[dict]:
    tasks = rows("SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 100),))
    for task in tasks:
        for key in ("phase_tokens", "model_route"):
            try:
                task[key] = json.loads(task.get(key) or "{}")
            except ValueError:
                task[key] = {}
        verification = rows("SELECT * FROM task_verifications WHERE task_id=?", (task["id"],))
        task["verification"] = json.loads(verification[0]["report"]) if verification else None
        plans = rows("SELECT status, plan, acceptance_criteria FROM task_plans WHERE task_id=?", (task["id"],))
        task["plan"] = json.loads(plans[0]["plan"]) if plans else None
        task["verification_attempts"] = rows("SELECT attempt, status, report, created_at FROM task_verification_attempts WHERE task_id=? ORDER BY attempt", (task["id"],))
        for attempt in task["verification_attempts"]:
            attempt["report"] = json.loads(attempt["report"])
        task["repair_runs"] = rows("SELECT attempt, status, retry_scope, reason, created_at, finished_at FROM task_repair_runs WHERE task_id=? ORDER BY attempt", (task["id"],))
        for repair in task["repair_runs"]:
            repair["retry_scope"] = json.loads(repair["retry_scope"] or "[]")
        task["checkpoints"] = rows(
            "SELECT sequence, phase, reason, workspace_hash, git_status, created_at FROM task_checkpoints WHERE task_id=? ORDER BY sequence DESC LIMIT 50",
            (task["id"],),
        )
        task["operations"] = rows(
            "SELECT execution_id, checkpoint_sequence, tool_call_id, tool, status, side_effect, result, started_at, finished_at FROM task_operations WHERE task_id=? ORDER BY started_at DESC LIMIT 100",
            (task["id"],),
        )
        for operation in task["operations"]:
            operation["result"] = json.loads(operation["result"]) if operation["result"] else None
        runs = rows("SELECT id, source, risk, confirmed, tool, status, input, output, started_at, finished_at, duration_ms FROM tool_runs WHERE task_id=? ORDER BY id", (task["id"],))
        for run in runs:
            for key in ("input", "output"):
                try:
                    run[key] = json.loads(run[key] or "{}")
                except ValueError:
                    run[key] = {"raw": run[key]}
        task["tool_runs"] = runs
        task["skill_runs"] = rows("SELECT name, path, content_chars, created_at FROM skill_runs WHERE task_id=? ORDER BY id", (task["id"],))
        task["model_runs"] = rows(
            "SELECT provider, model, phase, route_tier, task_type, route_confidence, input_tokens, output_tokens, total_tokens, "
            "estimated_cost_usd, duration_ms, success, error_type, retry_count, started_at FROM model_runs WHERE task_id=? ORDER BY id",
            (task["id"],),
        )
        phase_costs: dict[str, dict[str, float | int]] = {}
        for run in task["model_runs"]:
            phase = str(run.get("phase") or "analysis")
            summary = phase_costs.setdefault(phase, {"calls": 0, "tokens": 0, "duration_ms": 0, "estimated_cost_usd": 0.0})
            summary["calls"] = int(summary["calls"]) + 1
            summary["tokens"] = int(summary["tokens"]) + int(run.get("total_tokens") or 0)
            summary["duration_ms"] = int(summary["duration_ms"]) + int(run.get("duration_ms") or 0)
            summary["estimated_cost_usd"] = round(float(summary["estimated_cost_usd"]) + float(run.get("estimated_cost_usd") or 0), 8)
        task["phase_costs"] = phase_costs
        working = rows("SELECT state, updated_at FROM task_working_memory WHERE task_id=?", (task["id"],))
        task["working_memory"] = json.loads(working[0]["state"]) if working else None
    return tasks


@router.get("/database/backups")
def list_database_backups() -> list[dict]:
    return database_backups()


@router.post("/database/backups")
def create_database_backup() -> dict:
    result = backup_database()
    audit(None, "database_backup", result["name"], "ok")
    return result


@router.post("/database/restore/{name}")
def restore_database_backup(name: str) -> dict:
    try:
        result = restore_database(name)
        audit(None, "database_restore", name, "ok")
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
