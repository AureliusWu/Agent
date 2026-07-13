import json

from fastapi import APIRouter, Header, HTTPException

from .. import __version__
from ..config import settings
from ..database import audit, backup_database, database_backups, database_status, restore_database, rows
from ..provider import provider_health

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health() -> dict:
    db = database_status()
    return {"status": "ok" if db["status"] == "ok" else "error", "version": __version__, "database": db, "model": settings.model_name}


@router.get("/provider/health")
async def model_health(x_model_api_key: str | None = Header(default=None)) -> dict:
    return await provider_health(x_model_api_key)


@router.get("/audit")
def audit_logs(limit: int = 100) -> list[dict]:
    return rows("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),))


@router.get("/tasks/recent")
def recent_tasks(limit: int = 30) -> list[dict]:
    tasks = rows("SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 100),))
    for task in tasks:
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
        runs = rows("SELECT id, source, risk, confirmed, tool, status, input, output, started_at, finished_at, duration_ms FROM tool_runs WHERE task_id=? ORDER BY id", (task["id"],))
        for run in runs:
            for key in ("input", "output"):
                try:
                    run[key] = json.loads(run[key] or "{}")
                except ValueError:
                    run[key] = {"raw": run[key]}
        task["tool_runs"] = runs
        task["skill_runs"] = rows("SELECT name, path, content_chars, created_at FROM skill_runs WHERE task_id=? ORDER BY id", (task["id"],))
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
