from fastapi import APIRouter, Header, HTTPException

from .. import __version__
from ..config import settings
from ..database import audit, backup_database, database_backups, restore_database, rows
from ..provider import provider_health

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "database": str(settings.database_path), "model": settings.model_name}


@router.get("/provider/health")
async def model_health(x_model_api_key: str | None = Header(default=None)) -> dict:
    return await provider_health(x_model_api_key)


@router.get("/audit")
def audit_logs(limit: int = 100) -> list[dict]:
    return rows("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),))


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
