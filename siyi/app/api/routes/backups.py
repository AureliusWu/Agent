from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.full_backup import MAX_BACKUP_BYTES, export_complete_backup, restore_complete_backup


router = APIRouter(prefix="/api/backups", tags=["backups"])


@router.get("/complete")
def export_backup(include_sensitive: bool = False, administrator_confirmed: bool = False) -> Response:
    try:
        payload, _ = export_complete_backup(include_sensitive=include_sensitive, administrator_confirmed=administrator_confirmed)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    filename = f"siyi-complete-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(payload, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/complete/restore")
async def restore_backup(file: UploadFile = File(...), administrator_confirmed: bool = Form(default=False)) -> dict:
    payload = await file.read(MAX_BACKUP_BYTES + 1)
    try:
        return restore_complete_backup(payload, administrator_confirmed=administrator_confirmed)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
