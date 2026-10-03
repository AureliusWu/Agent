from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.voice import AudioInput, SpeechRequest, create_default_voice_service


router = APIRouter(prefix="/api/voice", tags=["voice"])
service = create_default_voice_service()
MAX_AUDIO_BYTES = 10 * 1024 * 1024


class TranscribeRequest(BaseModel):
    audio_base64: str = Field(min_length=1)
    mime_type: str = Field(default="application/octet-stream", min_length=1, max_length=120)
    language: str | None = Field(default=None, max_length=32)
    provider_id: str | None = Field(default=None, max_length=80)


class SynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    voice: str | None = Field(default=None, max_length=80)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    audio_format: str = Field(default="wav", min_length=2, max_length=12)
    provider_id: str | None = Field(default=None, max_length=80)


@router.get("/health")
async def voice_health() -> dict[str, object]:
    return await service.health()


@router.post("/transcribe")
async def transcribe(payload: TranscribeRequest) -> dict[str, object]:
    try:
        audio = base64.b64decode(payload.audio_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(400, "audio_base64 is invalid") from exc
    if not audio:
        raise HTTPException(400, "audio payload is empty")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio payload is too large")
    try:
        result = await service.transcribe(
            AudioInput(data=audio, mime_type=payload.mime_type, language=payload.language),
            provider_id=payload.provider_id,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "text": result.text,
        "provider_id": result.provider_id,
        "language": result.language,
        "duration_ms": result.duration_ms,
        "confidence": result.confidence,
    }


@router.post("/synthesize")
async def synthesize(payload: SynthesizeRequest) -> dict[str, object]:
    try:
        result = await service.synthesize(
            SpeechRequest(
                text=payload.text,
                voice=payload.voice,
                speed=payload.speed,
                audio_format=payload.audio_format,
            ),
            provider_id=payload.provider_id,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "audio_base64": base64.b64encode(result.data).decode("ascii"),
        "mime_type": result.mime_type,
        "provider_id": result.provider_id,
        "duration_ms": result.duration_ms,
    }
