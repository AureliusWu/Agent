from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.database import audit
from app.stt.manager import stt_manager
from app.stt.schemas import STTError
from app.voice.session_manager import VoiceSessionError, voice_session_manager


router = APIRouter(prefix="/api/stt", tags=["stt"])


class SettingsInput(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    model_id: str | None = None
    device: str | None = None
    compute_type: str | None = None
    vad: bool | None = None
    idle_unload_minutes: int | None = Field(default=None, ge=0, le=60)
    gpu_experimental: bool | None = None


class ModelInput(BaseModel):
    model_id: str = Field(min_length=1, max_length=32)


class ConfirmedModelInput(ModelInput):
    confirmed: bool = False


class CancelInput(BaseModel):
    request_id: str | None = Field(default=None, max_length=128)
    voice_session_id: str | None = Field(default=None, max_length=128)


class VoiceSessionInput(BaseModel):
    conversation_id: int = Field(ge=1)
    device_id: str = Field(default="", max_length=500)
    auto_send: bool = False


class RecordingStartedInput(BaseModel):
    device_id: str = Field(default="", max_length=500)


class RecordingLevelInput(BaseModel):
    level: float = Field(ge=0, le=1)


class MicrophoneSettingsInput(BaseModel):
    selected_device_id: str | None = Field(default=None, max_length=500)
    selected_device_label: str | None = Field(default=None, max_length=500)
    max_duration_ms: int | None = Field(default=None, ge=300, le=120000)
    min_duration_ms: int | None = Field(default=None, ge=300, le=120000)
    auto_send: bool | None = None
    shortcut: str | None = Field(default=None, max_length=100)


def _error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "STT_TRANSCRIPTION_FAILED")
    if code in {"VOICE_SESSION_NOT_FOUND", "STT_MODEL_MISSING", "MIC_DEVICE_NOT_FOUND"}:
        status_code = status.HTTP_404_NOT_FOUND
    elif code in {"VOICE_SESSION_CONFLICT", "STT_RESOURCE_LIMIT", "STT_DOWNLOAD_CONFIRMATION_REQUIRED", "STT_DELETE_CONFIRMATION_REQUIRED", "STT_ALREADY_CANCELLED", "RESOURCE_RAM_PRESSURE", "RESOURCE_VRAM_PRESSURE"}:
        status_code = status.HTTP_409_CONFLICT
    elif code in {"RECORDING_TOO_SHORT", "RECORDING_TOO_LONG", "STT_INVALID_AUDIO", "STT_NO_SPEECH", "STT_PERMISSION_DENIED"}:
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(status_code, {"code": code, "message": str(exc)})


def _windows_microphones() -> list[dict[str, str]]:
    script = (
        "$items=@(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue|Where-Object{"
        "$_.Class -eq 'AudioEndpoint' -and $_.FriendlyName -match 'Microphone|\\u9EA6\\u514B\\u98CE'}|"
        "ForEach-Object{@{id=$_.InstanceId;label=$_.FriendlyName;status=$_.Status}});$items|ConvertTo-Json -Compress"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not result.stdout.strip():
        return []
    decoded = json.loads(result.stdout)
    records = decoded if isinstance(decoded, list) else [decoded]
    return [{"id": str(item.get("id") or ""), "label": str(item.get("label") or ""), "status": str(item.get("status") or "unknown")} for item in records if isinstance(item, dict) and item.get("id")]


@router.get("/health")
async def health() -> dict[str, Any]:
    return stt_manager.health()


@router.get("/providers")
async def providers() -> list[dict[str, Any]]:
    return stt_manager.health()["providers"]


@router.get("/models")
async def models() -> list[dict[str, Any]]:
    return stt_manager.list_models()


@router.get("/models/{model_id}/download-preview")
async def download_preview(model_id: str) -> dict[str, Any]:
    try:
        return stt_manager.download_preview(model_id)
    except STTError as exc:
        raise _error(exc) from exc


@router.get("/models/{model_id}/download")
async def download_status(model_id: str) -> dict[str, Any]:
    try:
        return stt_manager.download_state(model_id)
    except STTError as exc:
        raise _error(exc) from exc


@router.get("/devices")
async def devices() -> list[dict[str, str]]:
    return await asyncio.to_thread(_windows_microphones)


@router.get("/status")
async def status_view() -> dict[str, Any]:
    return stt_manager.status()


@router.get("/metrics")
async def metrics() -> dict[str, Any]:
    return stt_manager.metrics()


@router.get("/settings")
async def settings() -> dict[str, Any]:
    return stt_manager.settings()


@router.put("/settings")
async def update_settings(payload: SettingsInput) -> dict[str, Any]:
    try:
        result = stt_manager.update_settings(payload.model_dump(exclude_none=True))
    except STTError as exc:
        raise _error(exc) from exc
    audit(None, "stt_settings_updated", "stt", "ok", {"provider": result["provider"], "model": result["model_id"], "device": result["device"]})
    return result


@router.post("/models/download", status_code=status.HTTP_202_ACCEPTED)
async def download_model(payload: ConfirmedModelInput) -> dict[str, Any]:
    try:
        return await stt_manager.start_download(payload.model_id, confirmed=payload.confirmed)
    except STTError as exc:
        raise _error(exc) from exc


@router.post("/models/download/cancel")
async def cancel_download(payload: ModelInput) -> dict[str, Any]:
    try:
        return await stt_manager.cancel_download(payload.model_id)
    except STTError as exc:
        raise _error(exc) from exc


@router.post("/models/load")
async def load_model(payload: ModelInput) -> dict[str, Any]:
    try:
        return await stt_manager.load_model(payload.model_id)
    except STTError as exc:
        raise _error(exc) from exc


@router.post("/models/unload")
async def unload_model(payload: ModelInput) -> dict[str, Any]:
    try:
        return await stt_manager.unload_model(payload.model_id)
    except STTError as exc:
        raise _error(exc) from exc


@router.delete("/models/{model_id}")
async def delete_model(model_id: str, confirmed: bool = False) -> dict[str, Any]:
    try:
        return await stt_manager.delete_model(model_id, confirmed=confirmed)
    except STTError as exc:
        raise _error(exc) from exc


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(payload: VoiceSessionInput) -> dict[str, Any]:
    try:
        return await voice_session_manager.create(conversation_id=payload.conversation_id, device_id=payload.device_id, auto_send=payload.auto_send)
    except VoiceSessionError as exc:
        raise _error(exc) from exc


@router.post("/sessions/{voice_session_id}/recording-started")
async def recording_started(voice_session_id: str, payload: RecordingStartedInput) -> dict[str, Any]:
    try:
        return await voice_session_manager.recording_started(voice_session_id, device_id=payload.device_id)
    except VoiceSessionError as exc:
        raise _error(exc) from exc


@router.post("/sessions/{voice_session_id}/recording-level")
async def recording_level(voice_session_id: str, payload: RecordingLevelInput) -> dict[str, Any]:
    try:
        return await voice_session_manager.recording_level(voice_session_id, level=payload.level)
    except VoiceSessionError as exc:
        raise _error(exc) from exc


@router.post("/sessions/{voice_session_id}/permission-denied")
async def microphone_permission_denied(voice_session_id: str) -> dict[str, Any]:
    try:
        return await voice_session_manager.permission_denied(voice_session_id)
    except VoiceSessionError as exc:
        raise _error(exc) from exc


@router.post("/sessions/{voice_session_id}/complete")
async def complete_session(voice_session_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    try:
        return await voice_session_manager.complete(voice_session_id, file)
    except VoiceSessionError as exc:
        raise _error(exc) from exc


@router.post("/sessions/{voice_session_id}/cancel")
async def cancel_session(voice_session_id: str) -> dict[str, Any]:
    try:
        return await voice_session_manager.cancel(voice_session_id)
    except VoiceSessionError as exc:
        raise _error(exc) from exc


@router.get("/sessions/{voice_session_id}")
async def get_session(voice_session_id: str) -> dict[str, Any]:
    try:
        return voice_session_manager.get(voice_session_id)
    except VoiceSessionError as exc:
        raise _error(exc) from exc


@router.post("/transcribe")
async def transcribe(voice_session_id: str = Form(..., min_length=16, max_length=80), file: UploadFile = File(...)) -> dict[str, Any]:
    """Controlled multipart-only entry point; never accepts local paths or URLs."""
    try:
        return await voice_session_manager.complete(voice_session_id, file)
    except VoiceSessionError as exc:
        raise _error(exc) from exc


@router.post("/cancel")
async def cancel(payload: CancelInput) -> dict[str, Any]:
    try:
        return await stt_manager.cancel(request_id=payload.request_id, voice_session_id=payload.voice_session_id)
    except STTError as exc:
        raise _error(exc) from exc


@router.get("/microphone/settings")
async def microphone_settings() -> dict[str, Any]:
    return voice_session_manager.microphone_settings()


@router.put("/microphone/settings")
async def update_microphone_settings(payload: MicrophoneSettingsInput) -> dict[str, Any]:
    try:
        return voice_session_manager.update_microphone_settings(payload.model_dump(exclude_none=True))
    except VoiceSessionError as exc:
        raise _error(exc) from exc
