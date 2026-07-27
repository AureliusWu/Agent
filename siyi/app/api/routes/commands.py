from fastapi import APIRouter

from app.commands.registry import command_catalog, validate_command
from app.database import audit
from app.schemas import CommandAuditRequest, CommandValidationRequest


router = APIRouter(prefix="/api/commands", tags=["commands"])


@router.get("")
def commands() -> dict:
    return {"commands": command_catalog()}


@router.post("/validate")
def validate(payload: CommandValidationRequest) -> dict:
    return validate_command(
        payload.text,
        has_conversation=payload.has_conversation,
        has_workspace=payload.has_workspace,
        running=payload.running,
        waiting_confirmation=payload.waiting_confirmation,
        recovering=payload.recovering,
        confirmed=payload.confirmed,
    )


@router.post("/audit")
def audit_command(payload: CommandAuditRequest) -> dict:
    audit(payload.conversation_id, "local_command", payload.command, payload.status, {"arguments_recorded": False})
    return {"status": "recorded"}
