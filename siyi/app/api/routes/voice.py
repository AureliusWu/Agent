from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.database import audit
from app.voice.events import voice_events
from app.voice.session_manager import VoiceSessionError, voice_session_manager


router = APIRouter(prefix="/api/voice", tags=["voice"])


class StopInput(BaseModel):
    voice_session_id: str | None = Field(default=None, max_length=80)


class TTSDispatchInput(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _stop_audit_details(result: dict, *, requested_session: bool) -> dict:
    """Return the narrow, content-free durable stop receipt.

    The endpoint result intentionally contains task/session identifiers for the
    authenticated renderer.  Audit records must not copy those identifiers,
    prompts, transcript text, or resource error details: the acceptance
    collector needs only aggregate convergence and idempotency facts.
    """

    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    target_count = _nonnegative_int(summary.get("target_count"))
    unresolved_count = _nonnegative_int(summary.get("unresolved_count"))
    status = str(result.get("status") or "UNKNOWN")
    return {
        "schema_version": 1,
        "scope": "session" if requested_session else "all",
        "requested_session": bool(requested_session),
        "sessions_targeted": _nonnegative_int(result.get("sessions_targeted")),
        "sessions_cancelled": _nonnegative_int(result.get("sessions_cancelled")),
        "target_count": target_count,
        "active_count": _nonnegative_int(summary.get("active_count")),
        "queue_active_count": _nonnegative_int(summary.get("queue_active_count")),
        "unresolved_count": unresolved_count,
        "settled": status == "CANCELLED" and unresolved_count == 0,
        "idempotent_no_active_target": status == "CANCELLED" and target_count == 0,
    }


@router.get("/events")
async def events(
    request: Request,
    voice_session_id: str = Query(min_length=16, max_length=80),
    after_id: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    try:
        voice_session_manager.get(voice_session_id)
    except VoiceSessionError as exc:
        raise HTTPException(404, {"code": exc.code, "message": str(exc)}) from exc
    cursor = max(after_id, int(last_event_id) if last_event_id and last_event_id.isdigit() else 0)

    async def stream():
        nonlocal cursor
        idle = 0
        while not await request.is_disconnected():
            available = voice_events(voice_session_id, cursor)
            if available:
                idle = 0
                for event in available:
                    cursor = int(event["id"])
                    yield f"id: {cursor}\nevent: {event['event']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event["event"] == "VOICE_SESSION_COMPLETED":
                        return
            else:
                idle += 1
                if idle % 40 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/stop")
async def stop(payload: StopInput) -> dict:
    try:
        result = await voice_session_manager.stop(
            payload.voice_session_id,
            reason="global_stop",
        )
        requested_session = payload.voice_session_id is not None
        audit(
            None,
            "voice_session_stop" if requested_session else "voice_global_stop",
            "session" if requested_session else "all",
            str(result.get("status") or "UNKNOWN"),
            _stop_audit_details(result, requested_session=requested_session),
        )
        return result
    except VoiceSessionError as exc:
        raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc


@router.post("/tts-dispatch-finished")
async def tts_dispatch_finished(payload: TTSDispatchInput) -> dict:
    """Close the bounded renderer-to-TTS handoff for a voice-bound task."""
    return {"task_id": payload.task_id, "sessions": await voice_session_manager.tts_dispatch_finished(payload.task_id)}
