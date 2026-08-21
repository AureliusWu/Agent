from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from app.database import connect, now_iso, rows, tts_idempotency_digest
from app.local_runtime.resource_coordinator import resource_coordinator
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
    temporary_audio_ttl_seconds = 15 * 60
    # A renderer crash cannot be relied upon to send its final interrupt.  The
    # reaper runs independently of future synthesis requests, keeping the
    # maximum retention of non-cache audio bounded to the TTL plus this small
    # polling interval.
    temporary_audio_reaper_interval_seconds = 15

    def __init__(self, cache: AudioCache | None = None) -> None:
        layout = ensure_runtime_layout(runtime_layout())
        self.cache = cache or AudioCache()
        self.temp = layout.temp / "tts"
        self.temp.mkdir(parents=True, exist_ok=True)
        self.providers = {"melotts": MeloTTSProvider(), "windows": WindowsTTSProvider()}
        self._active: dict[str, asyncio.Task] = {}
        self._audio_paths: dict[str, Path] = {}
        self._queue: deque[dict[str, Any]] = deque()
        # A request enters this set before synthesis starts and remains there
        # until playback reaches a terminal state.  It closes the small race
        # where an Agent has produced its final token while the frontend is
        # still synthesising/queueing its final sentence.
        #
        # The VoiceSessionManager is attached at runtime instead of imported
        # here.  Keeping this dependency inverted avoids a TTS <-> Voice
        # import cycle and lets isolated manager tests use their own session
        # manager.
        self._speak_intents: dict[str, str | None] = {}
        self._voice_session_manager: Any | None = None
        # A synchronous audio URL lookup can discover expired audio from a
        # thread-pool route.  Queue its lifecycle notification so the async
        # background reaper can still converge the related Voice Session.
        self._expired_temporary_audio: deque[tuple[str, str | None, str | None]] = deque()
        self._events: deque[dict[str, Any]] = deque(maxlen=2000)
        self._event_id = 0
        self._condition = asyncio.Condition()

    def set_voice_session_manager(self, manager: Any) -> None:
        """Attach the coordinating voice session manager without importing it.

        TTS is also used on its own, so notifications remain a best-effort
        integration concern and must never make audio synthesis/playback fail.
        """
        self._voice_session_manager = manager

    def request_from_payload(self, payload: dict[str, Any]) -> SynthesisRequest:
        """Build a request that inherits the user's persisted TTS settings.

        API callers may still explicitly override a value for a one-off
        synthesis.  Automatic browser TTS intentionally omits these fields,
        so it must use the selected voice, speed, volume and sample rate.
        """
        return create_request(payload, defaults=self.settings())

    def cleanup_orphaned_temporary_audio(self) -> int:
        """Remove every controlled non-cache WAV left by a prior process.

        The sidecar is single-instance.  After a crash its in-memory ownership
        map is gone, so retaining even a fresh WAV risks leaving sensitive
        speech material on disk indefinitely.  Cached audio lives elsewhere
        and is deliberately not touched here.
        """
        removed = 0
        if not self.temp.exists():
            return removed
        root = self.temp.resolve()
        for candidate in self.temp.glob("*.wav"):
            try:
                if not candidate.is_file() or candidate.resolve().parent != root:
                    continue
                candidate.unlink()
                removed += 1
            except OSError:
                logging.getLogger("agent.tts").warning("failed to remove orphaned temporary audio: %s", candidate)
        self._audio_paths = {
            request_id: path
            for request_id, path in self._audio_paths.items()
            if path.is_file() and path.resolve().parent != root
        }
        # The request table must not advertise a no-longer-owned temporary
        # audio URL after a sidecar restart. Cached rows have a durable cache
        # id and remain available by design.
        with connect() as db:
            db.execute(
                "UPDATE tts_requests SET status='CANCELLED',error_code='TTS_SIDECAR_RESTARTED',updated_at=? "
                "WHERE cache_id IS NULL AND status IN ('SYNTHESIZING','READY','QUEUED','PLAYING')",
                (now_iso(),),
            )
        return removed

    def cleanup_expired_temporary_audio(self, *, now: float | None = None) -> int:
        """Synchronously remove expired non-cache audio and queue its notices.

        This remains synchronous because ``audio_path`` runs from a regular
        FastAPI route.  The application's async reaper flushes the queued
        Voice Session lifecycle notices immediately afterwards; synthesis
        calls use :meth:`reap_expired_temporary_audio` directly.
        """
        return len(self._expire_temporary_audio(now=now))

    async def reap_expired_temporary_audio(self, *, now: float | None = None) -> int:
        """Run TTL cleanup and converge affected task-scoped Voice Sessions."""
        expired = self._expire_temporary_audio(now=now)
        while self._expired_temporary_audio:
            request_id, task_id, message_id = self._expired_temporary_audio.popleft()
            await self._emit(
                "tts.temporary_audio.expired",
                request_id,
                task_id,
                message_id,
                {"reason": "TTS_TEMPORARY_AUDIO_EXPIRED"},
            )
            await self._notify_voice_session(
                "tts_playback_stopped", task_id=task_id, request_id=request_id
            )
        return len(expired)

    def _expire_temporary_audio(self, *, now: float | None = None) -> list[str]:
        """Delete only stale files inside the managed temporary TTS root.

        The renderer normally acknowledges every playback terminal state.
        When it is killed or faults before that acknowledgement, a live
        sidecar still owns the request map and queue.  This method removes
        both the WAV and that in-memory ownership, rather than waiting for a
        later synthesis call or a sidecar restart.  Cache files live under a
        different root and are never inspected here.
        """
        current = time.time() if now is None else now
        deadline = current - self.temporary_audio_ttl_seconds
        root = self.temp.resolve()
        active_request_ids = set(self._active)
        request_records = {
            str(record["request_id"]): record
            for record in rows(
                "SELECT request_id,task_id,message_id,provider,status FROM tts_requests "
                "WHERE cache_id IS NULL AND status IN ('SYNTHESIZING','READY','QUEUED','PLAYING')"
            )
        }
        protected_request_ids = active_request_ids | {
            request_id
            for request_id, record in request_records.items()
            if record["status"] == "SYNTHESIZING"
        }

        stale_paths: set[Path] = set()
        for candidate in self.temp.glob("*.wav"):
            try:
                resolved = candidate.resolve()
                if not candidate.is_file() or resolved.parent != root:
                    continue
                # Never delete a provider's active output, including the
                # short interval before its task is recorded in _audio_paths.
                if any(resolved.name.startswith(f"{request_id}-") for request_id in protected_request_ids):
                    continue
                if resolved.stat().st_mtime <= deadline:
                    stale_paths.add(resolved)
            except OSError:
                continue

        if not stale_paths:
            return []

        request_by_path: dict[Path, str] = {}
        for request_id, path in self._audio_paths.items():
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in stale_paths and resolved.parent == root:
                request_by_path[resolved] = request_id

        # Retain an association even if a renderer fault left the process map
        # incomplete.  Provider IDs make the generated file name exact and
        # avoid guessing from user-controlled arbitrary prefixes.
        for request_id, record in request_records.items():
            provider = str(record.get("provider") or "")
            if not provider:
                continue
            expected_name = f"{request_id}-{provider}.wav"
            for path in stale_paths:
                if path.name == expected_name:
                    request_by_path[path] = request_id

        removed = 0
        expired_request_ids: set[str] = set()
        for path in stale_paths:
            try:
                path.unlink(missing_ok=True)
                removed += 1
                request_id = request_by_path.get(path)
                if request_id:
                    expired_request_ids.add(request_id)
            except OSError:
                # Windows can temporarily hold a playing WAV open.  Leave it
                # for the next reaper interval rather than marking it absent.
                continue

        for request_id in expired_request_ids:
            record = request_records.get(request_id)
            if record is None:
                continue
            self._audio_paths.pop(request_id, None)
            self._speak_intents.pop(request_id, None)
            self._queue = deque(
                item for item in self._queue if str(item.get("request_id")) != request_id
            )
            self._update_request(
                request_id,
                "CANCELLED",
                error_code="TTS_TEMPORARY_AUDIO_EXPIRED",
            )
            self._expired_temporary_audio.append(
                (request_id, str(record["task_id"]) if record.get("task_id") else None,
                 str(record["message_id"]) if record.get("message_id") else None)
            )
        return list(expired_request_ids)

    def has_pending_for_task(self, task_id: str) -> bool:
        """Whether an explicit spoken reply for *task_id* is still in flight."""
        return any(candidate_task == task_id for candidate_task in self._speak_intents.values())

    async def _notify_voice_session(self, method: str, *, task_id: str | None, request_id: str) -> None:
        """Send lifecycle metadata to VoiceSessionManager without a cycle.

        The payload is deliberately only task/request correlation data.  The
        voice event store is responsible for its own whitelist and never sees
        spoken text or an audio path here.
        """
        manager = self._voice_session_manager
        if manager is None or not task_id:
            return
        callback = getattr(manager, method, None)
        if callback is None:
            return
        try:
            result = callback(task_id=task_id, request_id=request_id)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logging.getLogger("agent.voice").exception(
                "failed to report TTS lifecycle event %s for task %s", method, task_id
            )

    def settings(self) -> dict[str, Any]:
        record = rows("SELECT * FROM tts_settings WHERE singleton=1")
        value = record[0] if record else {}
        return {
            "enabled": bool(value.get("enabled")),
            "provider": str(value.get("provider") or "windows"),
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
        # Only immediate interruption is implemented end-to-end.  Do not
        # persist a policy that the browser/player cannot honestly honour.
        if current["interrupt_policy"] != "IMMEDIATE":
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
        await self.reap_expired_temporary_audio()
        idempotency_digest = tts_idempotency_digest(request.idempotency_key)
        existing = rows("SELECT request_id,status,provider,cache_id,duration_ms,synthesis_ms,error_code FROM tts_requests WHERE idempotency_key=?", (idempotency_digest,))
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
            db.execute("INSERT INTO tts_requests(request_id,idempotency_key,task_id,message_id,status,sensitive,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (request.request_id, idempotency_digest, request.task_id, request.message_id, "SYNTHESIZING", int(normalized.sensitive), stamp, stamp))
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
        if resource_coordinator.recording_active:
            raise TTSManagerError("TTS playback is paused while the microphone is active", "TTS_INTERRUPTED_BY_VOICE_INPUT")
        if len(self._queue) >= self.max_queue_size:
            raise TTSManagerError("TTS playback queue is full", "TTS_QUEUE_FULL")
        # Register before the first await.  A task can finish while Windows
        # synthesis is still running; its Voice Session must wait for this
        # explicit spoken reply rather than emitting a premature completion.
        self._speak_intents[request.request_id] = request.task_id
        try:
            result = await self.synthesize(request)
        except asyncio.CancelledError:
            self._speak_intents.pop(request.request_id, None)
            await self._notify_voice_session(
                "tts_playback_stopped", task_id=request.task_id, request_id=request.request_id
            )
            raise
        except Exception:
            self._speak_intents.pop(request.request_id, None)
            await self._notify_voice_session(
                "tts_playback_stopped", task_id=request.task_id, request_id=request.request_id
            )
            raise
        result_request_id = str(result["request_id"])
        if result_request_id != request.request_id:
            # Idempotency may return a prior request id.  Keep the lifecycle
            # intent keyed to the id that playback endpoints will actually
            # receive, otherwise the voice session could wait forever.
            linked_task_id = self._speak_intents.pop(request.request_id, request.task_id)
            self._speak_intents[result_request_id] = linked_task_id
        if result_request_id not in self._speak_intents:
            # A stop can arrive while synthesis is running.  Do not append a
            # late item after the user has already cleared this task's queue.
            self._update_request(result_request_id, "CANCELLED")
            self._discard_uncached_audio(result_request_id)
            raise TTSManagerError("TTS playback was stopped before it could start", "TTS_ALREADY_CANCELLED")
        # The microphone can become active while a provider is synthesising.
        # There is no await between this check and queue insertion, so this
        # second half-duplex gate prevents a completed WAV from becoming a
        # newly playable queue item after recording has started.
        if resource_coordinator.recording_active:
            linked_task_id = self._speak_intents.pop(result_request_id, request.task_id)
            self._update_request(
                result_request_id,
                "CANCELLED",
                error_code="TTS_INTERRUPTED_BY_VOICE_INPUT",
            )
            # A sensitive/non-cache result must be removed from disk.  For a
            # cache-backed result, retain the shared non-sensitive cache but
            # remove this cancelled request's in-memory audio access.
            self._discard_uncached_audio(result_request_id)
            self._audio_paths.pop(result_request_id, None)
            await self._notify_voice_session(
                "tts_playback_stopped",
                task_id=linked_task_id,
                request_id=result_request_id,
            )
            raise TTSManagerError(
                "TTS playback was interrupted because microphone recording started",
                "TTS_INTERRUPTED_BY_VOICE_INPUT",
            )
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
        self.cleanup_expired_temporary_audio()
        path = self._audio_paths.get(request_id)
        if not path or not path.is_file():
            raise TTSManagerError("Audio is unavailable", "TTS_CACHE_UNAVAILABLE")
        roots = {self.cache.root, self.temp.resolve()}
        resolved = path.resolve()
        if resolved.parent not in roots:
            raise TTSManagerError("Audio path escaped the managed TTS directories", "TTS_PERMISSION_DENIED")
        return resolved

    def _discard_uncached_audio(self, request_id: str) -> None:
        """Remove only a non-cache playback file owned by this request."""
        record = rows("SELECT cache_id FROM tts_requests WHERE request_id=?", (request_id,))
        if record and record[0].get("cache_id"):
            return
        path = self._audio_paths.pop(request_id, None)
        if path is not None:
            path.unlink(missing_ok=True)

    async def playback_started(self, request_id: str) -> dict:
        item = next((item for item in self._queue if item["request_id"] == request_id), None)
        if not item:
            raise TTSManagerError("Queue item not found", "TTS_ALREADY_CANCELLED")
        item["status"] = "PLAYING"
        self._update_request(request_id, "PLAYING")
        await self._emit("tts.playback.started", request_id, item.get("task_id"), item.get("message_id"), {})
        await self._notify_voice_session(
            "tts_playback_started", task_id=item.get("task_id"), request_id=request_id
        )
        return dict(item)

    async def playback_finished(self, request_id: str, *, failed: bool = False) -> dict:
        item = next((item for item in self._queue if item["request_id"] == request_id), None)
        if not item:
            return {"request_id": request_id, "status": "CANCELLED"}
        self._queue.remove(item)
        status = "FAILED" if failed else "COMPLETED"
        self._update_request(request_id, status, error_code="TTS_PLAYBACK_FAILED" if failed else None)
        await self._emit("tts.playback.failed" if failed else "tts.playback.completed", request_id, item.get("task_id"), item.get("message_id"), {})
        self._speak_intents.pop(request_id, None)
        self._discard_uncached_audio(request_id)
        await self._notify_voice_session(
            "tts_playback_stopped" if failed else "tts_playback_completed",
            task_id=item.get("task_id"),
            request_id=request_id,
        )
        return {**item, "status": status}

    async def stop(self, *, task_id: str | None = None, clear_queue: bool = True) -> dict:
        targets = [request_id for request_id in self._active if task_id is None or _request_task(request_id) == task_id]
        target_tasks = {
            request_id: task
            for request_id in targets
            if (task := self._active.get(request_id)) is not None
        }
        stopped: dict[str, str | None] = {}
        retained_queue_ids = {
            str(item["request_id"])
            for item in self._queue
            if (task_id is None or item.get("task_id") == task_id)
        }
        # A request may be between ``speak`` registering intent and
        # ``synthesize`` creating its provider task.  Clear that intent too,
        # otherwise a late synthesis could reinsert playback after Stop.
        for request_id, linked_task_id in tuple(self._speak_intents.items()):
            if (task_id is None or linked_task_id == task_id) and (clear_queue or request_id not in retained_queue_ids):
                stopped[request_id] = linked_task_id
                self._speak_intents.pop(request_id, None)
        for request_id in targets:
            stopped[request_id] = _request_task(request_id)
            self._update_request(request_id, "CANCEL_REQUESTED")
            target_tasks[request_id].cancel()
        removed = 0
        if clear_queue:
            kept = deque()
            for item in self._queue:
                if task_id is None or item.get("task_id") == task_id:
                    removed += 1
                    stopped[item["request_id"]] = item.get("task_id")
                    self._update_request(item["request_id"], "CANCELLED")
                    self._speak_intents.pop(item["request_id"], None)
                    self._discard_uncached_audio(item["request_id"])
                else:
                    kept.append(item)
            self._queue = kept

        pending_tasks: set[asyncio.Task[Any]] = set()
        if target_tasks:
            _, pending_tasks = await asyncio.wait(
                set(target_tasks.values()),
                timeout=3.0,
            )
        provider_cancel_errors: list[str] = []
        if pending_tasks:
            results = await asyncio.gather(
                *(
                    provider.cancel(request_id)
                    for request_id, task in target_tasks.items()
                    if task in pending_tasks
                    for provider in self.providers.values()
                ),
                return_exceptions=True,
            )
            provider_cancel_errors = sorted(
                {type(result).__name__ for result in results if isinstance(result, BaseException)}
            )
            _, pending_tasks = await asyncio.wait(pending_tasks, timeout=2.5)

        provider_active: set[str] = set()
        for provider in self.providers.values():
            try:
                status = provider.get_status()
            except Exception as exc:
                provider_cancel_errors.append(type(exc).__name__)
                continue
            active_requests = status.get("active_requests") if isinstance(status, dict) else None
            if isinstance(active_requests, list):
                provider_active.update(str(value) for value in active_requests)
        unresolved = sorted(
            request_id
            for request_id, task in target_tasks.items()
            if task in pending_tasks or request_id in self._active or request_id in provider_active
        )
        for request_id in stopped:
            if request_id in unresolved:
                continue
            self._update_request(request_id, "CANCELLED")
            self._discard_uncached_audio(request_id)
        for request_id, linked_task_id in stopped.items():
            if request_id in unresolved:
                continue
            await self._notify_voice_session(
                "tts_playback_stopped", task_id=linked_task_id, request_id=request_id
            )
        settled = not unresolved
        status = "CANCELLED" if settled else "CANCEL_REQUESTED"
        payload = {
            "cancelled_synthesis": len(targets),
            "cleared_queue": removed,
            "settled": settled,
            "unresolved_requests": unresolved,
            "provider_errors": sorted(set(provider_cancel_errors)),
        }
        await self._emit("tts.stopped", "", task_id, None, payload)
        return {"status": status, **payload}

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
        self._speak_intents.clear()
        # A normal sidecar shutdown deserves the same privacy bound as a
        # restart.  This only visits the private temporary root, never cache.
        self.cleanup_orphaned_temporary_audio()

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


def create_request(payload: dict[str, Any], *, defaults: dict[str, Any] | None = None) -> SynthesisRequest:
    defaults = defaults or {}

    def configured(key: str, fallback: Any) -> Any:
        value = payload.get(key)
        return defaults.get(key, fallback) if value is None else value

    request_id = str(payload.get("request_id") or uuid.uuid4().hex)
    return SynthesisRequest(
        request_id=request_id,
        task_id=str(payload["task_id"]) if payload.get("task_id") else None,
        message_id=str(payload["message_id"]) if payload.get("message_id") else None,
        text=str(payload.get("text") or ""),
        voice=str(configured("voice", "") or ""),
        speed=float(configured("speed", 1.0)),
        volume=float(configured("volume", 1.0)),
        sample_rate=int(configured("sample_rate", 24000)),
        priority=str(payload.get("priority") or "NORMAL"),
        cache=bool(payload.get("cache", True)),
        idempotency_key=str(payload.get("idempotency_key") or request_id),
    )
