from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.database import audit, connect, now_iso
from app.local_runtime.model_manager import ModelManagerError, model_manager
from app.local_runtime.ollama_service_manager import OllamaServiceError, ollama_service_manager
from app.local_runtime.resource_coordinator import resource_coordinator


router = APIRouter(prefix="/api/local-models", tags=["local-models"])


class ServiceStartInput(BaseModel):
    executable: str | None = None
    timeout_seconds: float = Field(default=15, ge=1, le=60)


class ModelInput(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ModelLoadInput(ModelInput):
    keep_alive: str = "5m"


class DownloadInput(ModelInput):
    confirmed: bool = False


def _error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "LOCAL_MODEL_ERROR")
    status_code = status.HTTP_409_CONFLICT if code in {"DOWNLOAD_CONFIRMATION_REQUIRED", "PORT_CONFLICT", "EXTERNAL_PROCESS_PROTECTED", "ACTIVE_GENERATION", "RESOURCE_RAM_PRESSURE", "RESOURCE_VRAM_PRESSURE"} else status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(status_code, {"code": code, "message": str(exc)})


@router.get("/service")
async def service_status() -> dict:
    result = await ollama_service_manager().status()
    _persist_service(result)
    return result


@router.post("/service/start")
async def service_start(payload: ServiceStartInput) -> dict:
    try:
        result = await ollama_service_manager().start(executable=payload.executable, timeout_seconds=payload.timeout_seconds)
    except OllamaServiceError as exc:
        raise _error(exc) from exc
    audit(None, "ollama_service_start", "ollama", "ok", {"pid": result.get("managed_pid"), "mode": result.get("mode")})
    _persist_service(result)
    return result


@router.post("/service/stop")
async def service_stop() -> dict:
    try:
        result = await ollama_service_manager().stop()
    except OllamaServiceError as exc:
        raise _error(exc) from exc
    audit(None, "ollama_service_stop", "ollama", "ok", {"pid": result.get("stopped_pid")})
    _persist_service(result)
    return result


@router.get("/models")
async def models() -> list[dict]:
    try:
        return [_local_model(item) for item in await model_manager.list_models()]
    except ModelManagerError as exc:
        raise _error(exc) from exc


def _local_model(item: dict) -> dict:
    """Normalize Ollama's evolving payload into the v15 LocalModel contract."""
    model_id = str(item.get("model_id") or item.get("name") or item.get("model") or "").strip()
    return {
        "provider": str(item.get("provider") or "ollama"),
        "model_id": model_id,
        "display_name": str(item.get("display_name") or model_id),
        "size": max(0, int(item.get("size") or item.get("size_bytes") or 0)),
        "digest": str(item.get("digest") or ""),
        "quantization": str(item.get("quantization") or ""),
        "capabilities": dict(item.get("capabilities") or {}),
        "installed": bool(item.get("installed", True)),
        "loaded": bool(item.get("loaded")),
        "benchmark": item.get("benchmark"),
        # v14 compatibility alias. Existing desktop rows and third-party
        # callers can migrate without losing their stable key.
        "name": model_id,
        **{
            key: item[key]
            for key in (
                "modified_at",
                "parameter_size",
                "context_length",
                "size_vram",
                "expires_at",
                "recommended",
            )
            if key in item
        },
    }


@router.get("/running")
async def running_models() -> list[dict]:
    try:
        return await model_manager.running_models()
    except ModelManagerError as exc:
        raise _error(exc) from exc


@router.post("/load")
async def load_model(payload: ModelLoadInput) -> dict:
    try:
        result = await model_manager.preload(payload.model, payload.keep_alive)
    except ModelManagerError as exc:
        raise _error(exc) from exc
    audit(None, "local_model_load", payload.model, "ok", {"keep_alive": payload.keep_alive, "load_ms": result["load_ms"]})
    return result


@router.post("/unload")
async def unload_model(payload: ModelInput) -> dict:
    try:
        result = await model_manager.unload(payload.model)
    except ModelManagerError as exc:
        raise _error(exc) from exc
    audit(None, "local_model_unload", payload.model, "ok", {"resource_release_observed": result["resource_release_observed"]})
    return result


@router.post("/download", status_code=202)
async def download_model(payload: DownloadInput) -> dict:
    try:
        result = await model_manager.start_download(payload.model, confirmed=payload.confirmed)
    except ModelManagerError as exc:
        raise _error(exc) from exc
    audit(None, "local_model_download", payload.model, "accepted")
    return result


@router.get("/download/preview")
def download_preview(model: str) -> dict:
    return model_manager.download_preview(model)


@router.get("/download")
def download_status(model: str | None = None):
    return model_manager.download_status(model)


@router.post("/download/cancel")
async def cancel_download(payload: ModelInput) -> dict:
    return await model_manager.cancel_download(payload.model)


@router.get("/resources")
async def resources() -> dict:
    from app.providers.ollama import active_ollama_requests
    from app.stt.manager import stt_manager
    from app.tts.manager import tts_manager
    running = await model_manager.running_models()
    active = str(running[0].get("name") or running[0].get("model") or "") if running else None
    tts_status = tts_manager.status()
    stt_status = stt_manager.status()
    tts_pids = [
        int(pid)
        for provider in tts_status.get("providers", [])
        if isinstance(provider, dict)
        for pid in provider.get("active_pids", [])
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
    ]
    # Resource accounting follows the exact loopback listener configured for
    # this sidecar.  It must never sum every ollama.exe on the workstation.
    service = await ollama_service_manager().status()
    listener = int(service.get("listener_pid") or 0)
    ollama_pid = listener if service.get("api_healthy") and listener > 0 else None
    snapshot = resource_coordinator.snapshot(
        active_model=active,
        tts_provider=tts_manager.settings()["provider"],
        stt_provider=stt_manager.settings()["provider"],
        stt_worker_pid=stt_status.get("worker_pid"),
        ollama_pid=ollama_pid,
        tts_pids=tts_pids,
    )
    return {
        "policy": resource_coordinator.policy(),
        "snapshot": snapshot,
        "admission": resource_coordinator.admission_status(snapshot=snapshot),
        "active_model_requests": active_ollama_requests(),
        "tts_status": tts_status,
        "stt_status": stt_status,
        "ollama_listener_pid": ollama_pid,
        "active_tasks": active_ollama_requests() + int(tts_status["status"] != "IDLE"),
    }


def _persist_service(result: dict) -> None:
    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO ollama_runtime_state(singleton,mode,status,pid,process_identity,executable_hash,base_url,version,last_error,updated_at) VALUES(1,?,?,?,?,?,?,?,?,?)",
            (result.get("mode") or "none", result.get("status") or "UNKNOWN", result.get("managed_pid") or result.get("listener_pid"), None, result.get("executable_sha256"), result.get("base_url") or "http://127.0.0.1:11434", result.get("version"), result.get("error"), now_iso()),
        )
