from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from app.database import connect, now_iso, rows
from app.runtime_paths import ensure_runtime_layout, runtime_layout
from app.tts.cache import AudioCache
from app.tts.providers.base import TTSProviderError
from app.tts.providers.melotts import MeloTTSProvider
from app.tts.providers.windows_tts import WindowsTTSProvider
from app.tts.schemas import SynthesisRequest, SynthesisResult
from app.tts.text_normalizer import normalize_for_speech


class TTSManagerError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class TTSManager:
    max_queue_size = 1000
    def __init__(self, cache: AudioCache | None = None) -> None:
        layout = ensure_runtime_layout(runtime_layout())
        self.cache = cache or AudioCache()
        self.temp = layout.temp / "tts"
        self.temp.mkdir(parents=True, exist_ok=True)
        self.providers = {"melotts": MeloTTSProvider(), "windows": WindowsTTSProvider()}
        self._active: dict[str, asyncio.Task] = {}
        self._audio_paths: dict[str, Path] = {}
        self._queue: deque[dict[str, Any]] = deque()
        self._events: deque[dict[str, Any]] = deque(maxlen=2000)
        self._event_id = 0
        self._condition = asyncio.Condition()

    def settings(self) -> dict[str, Any]:
        record = rows("SELECT * FROM tts_settings WHERE singleton=1")
        value = record[0] if record else {}
        return {
            "enabled": bool(value.get("enabled")),
            "provider": str(value.get("provider") or "melotts"),
            "fallback_provider": str(value.get("fallback_provider") or "windows"),
            "allow_fallback": bool(value.get("allow_fallback", 1)),
            "voice": str(value.get("voice") or ""),
            "speed": float(value.get("speed") or 1.0),
            "volume": float(value.get("volume") or 1.0),
            "sample_rate": int(value.get("sample_rate") or 24000),
            "playback_mode": str(value.get("playback_mode") or "MANUAL"),
            "interrupt_policy": str(value.get("interrupt_policy") or "IMMEDIATE"),
            "cache_enabled": bool(value.get("cache_enabled", 1)),
        }

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = {**self.settings(), **payload}
        if current["provider"] not in self.providers or current["fallback_provider"] not in self.providers:
            raise TTSManagerError("Unknown TTS provider", "TTS_PROVIDER_UNAVAILABLE")
        if current["playback_mode"] not in {"AUTO", "MANUAL", "SYSTEM_ONLY", "OFF"}:
            raise TTSManagerError("Invalid playback mode", "TTS_INVALID_SETTINGS")
        if current["interrupt_policy"] not in {"IMMEDIATE", "AFTER_SENTENCE", "NEVER"}:
            raise TTSManagerError("Invalid interrupt policy", "TTS_INVALID_SETTINGS")
        with connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO tts_settings(singleton,enabled,provider,fallback_provider,allow_fallback,voice,speed,volume,sample_rate,playback_mode,interrupt_policy,cache_enabled,updated_at) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(bool(current["enabled"])), current["provider"], current["fallback_provider"], int(bool(current["allow_fallback"])), current["voice"], float(current["speed"]), float(current["volume"]), int(current["sample_rate"]), current["playback_mode"], current["interrupt_policy"], int(bool(current["cache_enabled"])), now_iso()),
            )
        return self.settings()

    async def health(self) -> dict[str, Any]:
        checks = await asyncio.gather(*(provider.health_check() for provider in self.providers.values()))
        return {"status": "ok" if any(item["status"] == "ok" for item in checks) else "unavailable", "providers": checks, "settings": self.settings()}

    async def voices(self, provider_id: str | None = None) -> list[dict]:
        targets = [self.providers[provider_id]] if provider_id in self.providers else list(self.providers.values())
        result: list[dict] = []
        for provider in targets:
            result.extend([{**voice, "provider": provider.id} for voice in await provider.list_voices()])
        return result

    async def synthesize(self, request: SynthesisRequest) -> dict[str, Any]:
        existing = rows("SELECT request_id,status,provider,cache_id,duration_ms,synthesis_ms,error_code FROM tts_requests WHERE idempotency_key=?", (request.idempotency_key,))
        if existing:
            record = existing[0]
            available = record["status"] in {"READY", "QUEUED", "PLAYING", "COMPLETED"} and (bool(record.get("cache_id")) or record["request_id"] in self._audio_paths)
            return SynthesisResult(record["request_id"], record.get("provider") or "", record["status"], f"/api/tts/audio/{record['request_id']}" if available else None, int(record.get("duration_ms") or 0), request.sample_rate, bool(record.get("cache_id")), float(record.get("synthesis_ms") or 0), {"code": record["error_code"]} if record.get("error_code") else None).as_dict()
        normalized = normalize_for_speech(request.text)
        if not normalized.text:
            raise TTSManagerError("No speakable text remained after normalization", "TTS_INVALID_TEXT")
        settings = self.settings()
        provider_ids = [settings["provider"]]
        if settings["allow_fallback"] and settings["fallback_provider"] not in provider_ids:
            provider_ids.append(settings["fallback_provider"])
        stamp = now_iso()
        with connect() as db:
            db.execute("INSERT INTO tts_requests(request_id,idempotency_key,task_id,message_id,status,sensitive,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (request.request_id, request.idempotency_key, request.task_id, request.message_id, "SYNTHESIZING", int(normalized.sensitive), stamp, stamp))
        await self._emit("tts.synthesis.started", request.request_id, request.task_id, request.message_id, {"sensitive": normalized.sensitive})
        last_error: TTSProviderError | None = None
        for index, provider_id in enumerate(provider_ids):
            provider = self.providers[provider_id]
            cache_id = AudioCache.key(provider=provider.id, model_version=provider.version, voice=request.voice, normalized_text=normalized.text, speed=request.speed, sample_rate=request.sample_rate)
            if request.cache and settings["cache_enabled"] and not normalized.sensitive:
                hit = self.cache.lookup(cache_id)
                if hit:
                    self._audio_paths[request.request_id] = Path(hit["path"])
                    self._update_request(request.request_id, "READY", provider=provider.id, cache_id=cache_id, duration_ms=int(hit["duration_ms"]), synthesis_ms=0)
                    await self._emit("tts.synthesis.ready", request.request_id, request.task_id, request.message_id, {"provider": provider.id, "cached": True})
                    return SynthesisResult(request.request_id, provider.id, "READY", f"/api/tts/audio/{request.request_id}", int(hit["duration_ms"]), int(hit["sample_rate"]), True, 0).as_dict()
            output = self.temp / f"{request.request_id}-{provider.id}.wav"
            adjusted = SynthesisRequest(**{**request.__dict__, "text": normalized.text})
            try:
                task = asyncio.create_task(provider.synthesize(adjusted, output))
                self._active[request.request_id] = task
                metrics = await asyncio.wait_for(task, timeout=120)
                if request.cache and settings["cache_enabled"] and not normalized.sensitive:
                    path = self.cache.store(cache_id, output, provider=provider.id, model_version=provider.version, voice=request.voice, duration_ms=int(metrics["duration_ms"]), sample_rate=int(metrics["sample_rate"]))
                    output.unlink(missing_ok=True)
                    cached = True
                else:
                    path = output
                    cached = False
                self._audio_paths[request.request_id] = path
                self._update_request(request.request_id, "READY", provider=provider.id, cache_id=cache_id if cached else None, duration_ms=int(metrics["duration_ms"]), synthesis_ms=float(metrics["synthesis_ms"]))
                await self._emit("tts.synthesis.ready", request.request_id, request.task_id, request.message_id, {"provider": provider.id, "cached": cached, "fallback": index > 0})
                return SynthesisResult(request.request_id, provider.id, "READY", f"/api/tts/audio/{request.request_id}", int(metrics["duration_ms"]), int(metrics["sample_rate"]), cached, float(metrics["synthesis_ms"])).as_dict()
            except TTSProviderError as exc:
                last_error = exc
                output.unlink(missing_ok=True)
            except asyncio.TimeoutError:
                await provider.cancel(request.request_id)
                last_error = TTSProviderError("TTS synthesis timed out", "TTS_SYNTHESIS_TIMEOUT")
                output.unlink(missing_ok=True)
            finally:
                self._active.pop(request.request_id, None)
        code = last_error.code if last_error else "TTS_PROVIDER_UNAVAILABLE"
        self._update_request(request.request_id, "FAILED", error_code=code)
        await self._emit("tts.synthesis.failed", request.request_id, request.task_id, request.message_id, {"code": code})
        raise TTSManagerError(str(last_error or "No TTS provider is available"), code)

    async def speak(self, request: SynthesisRequest) -> dict[str, Any]:
        if len(self._queue) >= self.max_queue_size:
            raise TTSManagerError("TTS playback queue is full", "TTS_QUEUE_FULL")
        result = await self.synthesize(request)
        item = {"request_id": result["request_id"], "task_id": request.task_id, "message_id": request.message_id, "status": "QUEUED", "audio_url": result["audio_url"], "duration_ms": result["duration_ms"], "volume": request.volume, "priority": request.priority}
        priority = {"SYSTEM": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}.get(request.priority, 2)
        position = next((index for index, queued in enumerate(self._queue) if {"SYSTEM": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}.get(str(queued.get("priority")), 2) > priority), len(self._queue))
        self._queue.insert(position, item)
        self._update_request(request.request_id, "QUEUED")
        await self._emit("tts.queue.added", request.request_id, request.task_id, request.message_id, {"position": len(self._queue)})
        return {**result, "status": "QUEUED", "queue_position": len(self._queue)}

    def queue(self) -> list[dict[str, Any]]:
        return list(self._queue)

    def audio_path(self, request_id: str) -> Path:
        path = self._audio_paths.get(request_id)
        if not path or not path.is_file():
            raise TTSManagerError("Audio is unavailable", "TTS_CACHE_UNAVAILABLE")
        roots = {self.cache.root, self.temp.resolve()}
        resolved = path.resolve()
        if resolved.parent not in roots:
            raise TTSManagerError("Audio path escaped the managed TTS directories", "TTS_PERMISSION_DENIED")
        return resolved

    async def playback_started(self, request_id: str) -> dict:
        item = next((item for item in self._queue if item["request_id"] == request_id), None)
        if not item:
            raise TTSManagerError("Queue item not found", "TTS_ALREADY_CANCELLED")
        item["status"] = "PLAYING"
        self._update_request(request_id, "PLAYING")
        await self._emit("tts.playback.started", request_id, item.get("task_id"), item.get("message_id"), {})
        return dict(item)

    async def playback_finished(self, request_id: str, *, failed: bool = False) -> dict:
        item = next((item for item in self._queue if item["request_id"] == request_id), None)
        if not item:
            return {"request_id": request_id, "status": "CANCELLED"}
        self._queue.remove(item)
        status = "FAILED" if failed else "COMPLETED"
        self._update_request(request_id, status, error_code="TTS_PLAYBACK_FAILED" if failed else None)
        await self._emit("tts.playback.failed" if failed else "tts.playback.completed", request_id, item.get("task_id"), item.get("message_id"), {})
        record = rows("SELECT cache_id FROM tts_requests WHERE request_id=?", (request_id,))
        if record and not record[0].get("cache_id"):
            path = self._audio_paths.pop(request_id, None)
            if path is not None:
                path.unlink(missing_ok=True)
        return {**item, "status": status}

    async def stop(self, *, task_id: str | None = None, clear_queue: bool = True) -> dict:
        targets = [request_id for request_id in self._active if task_id is None or _request_task(request_id) == task_id]
        for request_id in targets:
            for provider in self.providers.values():
                await provider.cancel(request_id)
            task = self._active.get(request_id)
            if task:
                task.cancel()
            self._update_request(request_id, "CANCELLED")
        removed = 0
        if clear_queue:
            kept = deque()
            for item in self._queue:
                if task_id is None or item.get("task_id") == task_id:
                    removed += 1
                    self._update_request(item["request_id"], "CANCELLED")
                    record = rows("SELECT cache_id FROM tts_requests WHERE request_id=?", (item["request_id"],))
                    if not record or not record[0].get("cache_id"):
                        path = self._audio_paths.pop(item["request_id"], None)
                        if path is not None:
                            path.unlink(missing_ok=True)
                else:
                    kept.append(item)
            self._queue = kept
        await self._emit("tts.stopped", "", task_id, None, {"cancelled_synthesis": len(targets), "cleared_queue": removed})
        return {"status": "CANCELLED", "cancelled_synthesis": len(targets), "cleared_queue": removed}

    async def interrupt(self, *, task_id: str | None = None) -> dict:
        return await self.stop(task_id=task_id, clear_queue=True)

    async def clear_queue(self, *, task_id: str | None = None) -> dict:
        return await self.stop(task_id=task_id, clear_queue=True)

    async def shutdown(self) -> None:
        await self.stop(clear_queue=True)
        await asyncio.gather(*(provider.unload() for provider in self.providers.values()), return_exceptions=True)
        for request_id, path in tuple(self._audio_paths.items()):
            record = rows("SELECT cache_id FROM tts_requests WHERE request_id=?", (request_id,))
            if not record or not record[0].get("cache_id"):
                path.unlink(missing_ok=True)
                self._audio_paths.pop(request_id, None)

    def status(self) -> dict:
        return {"status": "PLAYING" if any(item["status"] == "PLAYING" for item in self._queue) else ("QUEUED" if self._queue else ("SYNTHESIZING" if self._active else "IDLE")), "active_synthesis": list(self._active), "queue_length": len(self._queue), "current": next((dict(item) for item in self._queue if item["status"] == "PLAYING"), None), "providers": [provider.get_status() for provider in self.providers.values()]}

    def metrics(self) -> dict:
        return {"providers": {key: provider.get_metrics() for key, provider in self.providers.items()}, "cache_entries": len(self.cache.list()), "queue_length": len(self._queue)}

    def cache_list(self) -> list[dict]:
        return self.cache.list()

    def cache_delete(self, cache_id: str) -> bool:
        return self.cache.delete(cache_id)

    def cache_clear(self) -> int:
        return self.cache.clear()

    def events_after(self, after_id: int) -> list[dict]:
        return [event for event in self._events if int(event["id"]) > after_id]

    async def wait_for_events(self, after_id: int, timeout: float = 10) -> list[dict]:
        existing = self.events_after(after_id)
        if existing:
            return existing
        async with self._condition:
            try:
                await asyncio.wait_for(self._condition.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return []
        return self.events_after(after_id)

    async def _emit(self, event: str, request_id: str, task_id: str | None, message_id: str | None, payload: dict) -> None:
        self._event_id += 1
        self._events.append({"id": self._event_id, "event": event, "request_id": request_id, "task_id": task_id, "message_id": message_id, "payload": payload, "created_at": now_iso()})
        async with self._condition:
            self._condition.notify_all()

    def _update_request(self, request_id: str, status: str, **values: Any) -> None:
        assignments = ["status=?", "updated_at=?"]
        params: list[Any] = [status, now_iso()]
        for key in ("provider", "cache_id", "duration_ms", "synthesis_ms", "error_code"):
            if key in values:
                assignments.append(f"{key}=?")
                params.append(values[key])
        params.append(request_id)
        with connect() as db:
            db.execute(f"UPDATE tts_requests SET {','.join(assignments)} WHERE request_id=?", tuple(params))


def _request_task(request_id: str) -> str | None:
    record = rows("SELECT task_id FROM tts_requests WHERE request_id=?", (request_id,))
    return str(record[0]["task_id"]) if record and record[0].get("task_id") else None


tts_manager = TTSManager()


def create_request(payload: dict[str, Any]) -> SynthesisRequest:
    request_id = str(payload.get("request_id") or uuid.uuid4().hex)
    return SynthesisRequest(
        request_id=request_id,
        task_id=str(payload["task_id"]) if payload.get("task_id") else None,
        message_id=str(payload["message_id"]) if payload.get("message_id") else None,
        text=str(payload.get("text") or ""),
        voice=str(payload.get("voice") or ""),
        speed=float(payload.get("speed") or 1.0),
        volume=float(payload.get("volume") if payload.get("volume") is not None else 1.0),
        sample_rate=int(payload.get("sample_rate") or 24000),
        priority=str(payload.get("priority") or "NORMAL"),
        cache=bool(payload.get("cache", True)),
        idempotency_key=str(payload.get("idempotency_key") or request_id),
    )
