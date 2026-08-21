from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.database import audit
from app.tts.manager import TTSManagerError, tts_manager


router = APIRouter(prefix="/api/tts", tags=["tts"])


class TTSInput(BaseModel):
    request_id: str | None = Field(default=None, max_length=128)
    task_id: str | None = Field(default=None, max_length=128)
    message_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=256)
    text: str = Field(min_length=1, max_length=4000)
    voice: str | None = Field(default=None, max_length=200)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    volume: float | None = Field(default=None, ge=0.0, le=1.0)
    sample_rate: int | None = Field(default=None, ge=8000, le=48000)
    priority: str = Field(default="NORMAL", pattern="^(LOW|NORMAL|HIGH|SYSTEM)$")
    cache: bool = True


class StopInput(BaseModel):
    task_id: str | None = Field(default=None, max_length=128)


class SettingsInput(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    fallback_provider: str | None = None
    allow_fallback: bool | None = None
    voice: str | None = Field(default=None, max_length=200)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    volume: float | None = Field(default=None, ge=0.0, le=1.0)
    sample_rate: int | None = Field(default=None, ge=8000, le=48000)
    playback_mode: str | None = None
    interrupt_policy: str | None = None
    cache_enabled: bool | None = None


def _http_error(exc: TTSManagerError) -> HTTPException:
    code = exc.code
    if code in {"TTS_INVALID_TEXT", "TTS_INVALID_VOICE", "TTS_INVALID_SETTINGS"}:
        status = 422
    elif code in {"TTS_ALREADY_CANCELLED", "TTS_QUEUE_FULL", "TTS_INTERRUPTED_BY_VOICE_INPUT"}:
        status = 409
    elif code == "TTS_PERMISSION_DENIED":
        status = 403
    else:
        status = 503
    return HTTPException(status, {"code": code, "message": str(exc)})


@router.get("/health")
async def health() -> dict:
    return await tts_manager.health()


@router.get("/providers")
async def providers() -> list[dict]:
    health_result = await tts_manager.health()
    return health_result["providers"]


@router.get("/voices")
async def voices(provider: str | None = None) -> list[dict]:
    return await tts_manager.voices(provider)


@router.get("/status")
def status() -> dict:
    return tts_manager.status()


@router.get("/metrics")
def metrics() -> dict:
    return tts_manager.metrics()


@router.get("/settings")
def settings() -> dict:
    return tts_manager.settings()


@router.put("/settings")
def update_settings(payload: SettingsInput) -> dict:
    try:
        result = tts_manager.update_settings(payload.model_dump(exclude_none=True))
    except TTSManagerError as exc:
        raise _http_error(exc) from exc
    audit(None, "tts_settings_updated", "tts", "ok", {"provider": result["provider"], "playback_mode": result["playback_mode"]})
    return result


@router.post("/synthesize")
async def synthesize(payload: TTSInput) -> dict:
    try:
        return await tts_manager.synthesize(tts_manager.request_from_payload(payload.model_dump(exclude_none=True)))
    except TTSManagerError as exc:
        raise _http_error(exc) from exc


@router.post("/speak")
async def speak(payload: TTSInput) -> dict:
    try:
        return await tts_manager.speak(tts_manager.request_from_payload(payload.model_dump(exclude_none=True)))
    except TTSManagerError as exc:
        raise _http_error(exc) from exc


@router.post("/stop")
async def stop(payload: StopInput) -> dict:
    return await tts_manager.stop(task_id=payload.task_id)


@router.post("/interrupt")
async def interrupt(payload: StopInput) -> dict:
    return await tts_manager.interrupt(task_id=payload.task_id)


@router.post("/queue/clear")
async def clear_queue(payload: StopInput) -> dict:
    return await tts_manager.clear_queue(task_id=payload.task_id)


@router.get("/queue")
def queue() -> list[dict]:
    return tts_manager.queue()


@router.post("/playback/{request_id}/start")
async def playback_start(request_id: str) -> dict:
    try:
        return await tts_manager.playback_started(request_id)
    except TTSManagerError as exc:
        raise _http_error(exc) from exc


@router.post("/playback/{request_id}/complete")
async def playback_complete(request_id: str, failed: bool = False) -> dict:
    return await tts_manager.playback_finished(request_id, failed=failed)


@router.get("/audio/{request_id}")
def audio(request_id: str) -> FileResponse:
    try:
        path = tts_manager.audio_path(request_id)
    except TTSManagerError as exc:
        raise _http_error(exc) from exc
    return FileResponse(
        path,
        media_type="audio/wav",
        filename="speech.wav",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@router.get("/cache")
def cache() -> list[dict]:
    return tts_manager.cache_list()


@router.delete("/cache")
def clear_cache() -> dict:
    return {"deleted": tts_manager.cache_clear()}


@router.delete("/cache/{cache_id}")
def delete_cache(cache_id: str) -> dict:
    return {"cache_id": cache_id, "deleted": tts_manager.cache_delete(cache_id)}


@router.get("/events")
async def events(after_id: int = Query(default=0, ge=0)) -> StreamingResponse:
    async def generate():
        cursor = after_id
        while True:
            batch = await tts_manager.wait_for_events(cursor, timeout=10)
            if not batch:
                yield ": keepalive\n\n"
                continue
            for event in batch:
                cursor = max(cursor, int(event["id"]))
                yield f"id: {event['id']}\nevent: {event['event']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")
