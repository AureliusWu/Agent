from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from app.database import connect, now_iso, rows
from app.local_runtime.resource_coordinator import resource_coordinator
from app.stt.manager import STTManager, stt_manager
from app.stt.schemas import DEFAULT_STT_MODEL_ID, STTError
from app.stt.temporary_storage import TemporaryAudioStore
from app.tts.manager import TTSManager, tts_manager

from .events import emit_voice_event


class VoiceSessionError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


ACTIVE_STATES = {"REQUESTING_PERMISSION", "RECORDING", "AUDIO_PROCESSING", "TRANSCRIBING", "REVIEWING", "QUEUED_FOR_AGENT", "AGENT_RUNNING", "TTS_PLAYING", "CANCEL_REQUESTED"}
TERMINAL_STATES = {"COMPLETED", "CANCELLED", "FAILED"}
CANCELLING_STATES = {"CANCEL_REQUESTED", "CANCELLED"}
# Only these states own the microphone/STT reservation.  An older voice
# message may still be waiting for an Agent or being read aloud when the user
# starts a new recording; that must interrupt its TTS, not reject the new
# microphone request.
CAPTURE_ACTIVE_STATES = {"REQUESTING_PERMISSION", "RECORDING", "AUDIO_PROCESSING", "TRANSCRIBING", "REVIEWING", "CANCEL_REQUESTED"}


class VoiceSessionManager:
    """One local voice session at a time, deliberately separate from Agent tasks."""

    # The Agent can finish on the server before the renderer has received the
    # final streaming delta and registered its browser-owned TTS request.  A
    # bounded reservation bridges that cross-process interval without turning
    # a lost renderer into a permanently active voice session.
    _TTS_DISPATCH_GRACE_SECONDS = 120.0
    _TASK_CANCELLATION_SETTLE_SECONDS = 1.0

    def __init__(self, *, stt: STTManager | None = None, tts: TTSManager | None = None, storage: TemporaryAudioStore | None = None) -> None:
        self.stt = stt or stt_manager
        self.tts = tts or tts_manager
        self.storage = storage or self.stt.audio_store
        self._lock = asyncio.Lock()
        # Keep durable SSE metadata useful without turning an animation-frame
        # level meter into a high-frequency database writer.
        self._last_recording_level_at: dict[str, float] = {}
        self._tts_dispatch_reservations: set[str] = set()
        self._tts_dispatch_timeouts: dict[str, asyncio.TimerHandle] = {}
        # TTS owns audio playback; this manager owns the cross-layer session
        # state.  Register a callback rather than importing Voice from TTS so
        # the modules remain acyclic and test managers can be isolated.
        attach = getattr(self.tts, "set_voice_session_manager", None)
        if callable(attach):
            attach(self)

    def microphone_settings(self) -> dict[str, Any]:
        record = rows("SELECT * FROM microphone_settings WHERE singleton=1")
        value = record[0] if record else {}
        return {
            "selected_device_id": value.get("selected_device_id") or "",
            "selected_device_label": value.get("selected_device_label") or "",
            "max_duration_ms": int(value.get("max_duration_ms") or 120000),
            "min_duration_ms": int(value.get("min_duration_ms") or 300),
            "auto_send": bool(value.get("auto_send")),
            "shortcut": value.get("shortcut") or "Space",
        }

    def update_microphone_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = {**self.microphone_settings(), **payload}
        if not 300 <= int(current["min_duration_ms"]) <= int(current["max_duration_ms"]) <= 120000:
            raise VoiceSessionError("Invalid recording duration limits", "RECORDING_TOO_LONG")
        if str(current["shortcut"]) not in {"Space", "Enter", "Alt+R"}:
            raise VoiceSessionError("Unsupported voice shortcut", "VOICE_SHORTCUT_INVALID")
        with connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO microphone_settings(singleton,selected_device_id,selected_device_label,max_duration_ms,min_duration_ms,auto_send,shortcut,updated_at) VALUES(1,?,?,?,?,?,?,?)",
                (str(current["selected_device_id"]), str(current["selected_device_label"]), int(current["max_duration_ms"]), int(current["min_duration_ms"]), int(bool(current["auto_send"])), str(current["shortcut"]), now_iso()),
            )
        return self.microphone_settings()

    async def create(self, *, conversation_id: int, device_id: str = "", auto_send: bool = False) -> dict[str, Any]:
        if not rows("SELECT id FROM conversations WHERE id=?", (conversation_id,)):
            raise VoiceSessionError("Conversation not found", "VOICE_SESSION_NOT_FOUND")
        # Check before and after interruption.  This preserves one active
        # capture, while allowing a new user recording to interrupt a prior
        # task's output TTS as required by the half-duplex policy.
        async with self._lock:
            active = rows("SELECT voice_session_id FROM voice_sessions WHERE state IN ({}) LIMIT 1".format(",".join("?" for _ in CAPTURE_ACTIVE_STATES)), tuple(CAPTURE_ACTIVE_STATES))
            if active:
                raise VoiceSessionError("Another voice session is active", "VOICE_SESSION_CONFLICT")
        admission = resource_coordinator.assess_admission("voice")
        if not admission["allowed"]:
            # Reject before interrupting an existing reply.  Text input and
            # existing work remain available; only the new capture is denied.
            raise VoiceSessionError(
                str(admission["reason"] or "Voice input was refused due to resource pressure"),
                str(admission["reason_code"] or "VOICE_SESSION_CONFLICT"),
            )
        await self.tts.interrupt()
        async with self._lock:
            active = rows("SELECT voice_session_id FROM voice_sessions WHERE state IN ({}) LIMIT 1".format(",".join("?" for _ in CAPTURE_ACTIVE_STATES)), tuple(CAPTURE_ACTIVE_STATES))
            if active:
                raise VoiceSessionError("Another voice session is active", "VOICE_SESSION_CONFLICT")
            voice_session_id = uuid.uuid4().hex
            if not resource_coordinator.acquire_voice_session(voice_session_id):
                raise VoiceSessionError("Voice resources are busy", "VOICE_SESSION_CONFLICT")
            stamp = now_iso()
            try:
                with connect() as db:
                    db.execute(
                        "INSERT INTO voice_sessions(voice_session_id,conversation_id,microphone_device_id,auto_send,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (voice_session_id, conversation_id, device_id, int(bool(auto_send)), "REQUESTING_PERMISSION", stamp, stamp),
                    )
                emit_voice_event(voice_session_id, "MIC_PERMISSION", {"status": "REQUESTED"})
                return self.get(voice_session_id)
            except Exception:
                resource_coordinator.release_voice_session(voice_session_id)
                raise

    async def recording_started(self, voice_session_id: str, *, device_id: str = "") -> dict[str, Any]:
        async with self._lock:
            session = self._require(voice_session_id, {"REQUESTING_PERMISSION", "CREATED"})
            self._update(voice_session_id, "RECORDING", microphone_device_id=device_id or session.get("microphone_device_id"), recording_started_at=now_iso())
            emit_voice_event(voice_session_id, "RECORDING_STARTED", {"device_id": device_id})
            self._last_recording_level_at[voice_session_id] = 0.0
            return self.get(voice_session_id)

    async def recording_level(self, voice_session_id: str, *, level: float) -> dict[str, Any]:
        """Record a bounded, quantized microphone level for the SSE timeline.

        The renderer displays its meter locally at animation-frame cadence.
        This method accepts at most one metadata sample per second so event
        persistence remains a diagnostic timeline rather than audio telemetry.
        """
        normalized = max(0.0, min(1.0, float(level)))
        async with self._lock:
            self._require(voice_session_id, {"RECORDING"})
            current = time.monotonic()
            previous = self._last_recording_level_at.get(voice_session_id, 0.0)
            if current - previous >= 1.0:
                self._last_recording_level_at[voice_session_id] = current
                emit_voice_event(voice_session_id, "RECORDING_LEVEL", {"level": round(normalized, 2)})
            return self.get(voice_session_id)

    async def permission_denied(self, voice_session_id: str) -> dict[str, Any]:
        """Persist a microphone denial without storing browser or device details."""
        async with self._lock:
            self._require(voice_session_id, {"REQUESTING_PERMISSION"})
            self._update(voice_session_id, "FAILED", error_code="STT_PERMISSION_DENIED")
            emit_voice_event(voice_session_id, "MIC_PERMISSION", {"status": "DENIED"})
            emit_voice_event(voice_session_id, "VOICE_SESSION_COMPLETED", {"status": "FAILED"})
            resource_coordinator.release_voice_session(voice_session_id)
            return self.get(voice_session_id)

    async def complete(self, voice_session_id: str, upload: UploadFile) -> dict[str, Any]:
        stt_reserved = False
        processing_started = False
        async with self._lock:
            self._require(voice_session_id, {"RECORDING", "REQUESTING_PERMISSION"})
            self._update(voice_session_id, "AUDIO_PROCESSING", recording_ended_at=now_iso())
            processing_started = True
        try:
            settings = self._stt_event_settings()
            admission = resource_coordinator.assess_admission(
                "stt",
                requires_gpu=settings.get("device") == "cuda",
            )
            if not admission["allowed"]:
                raise VoiceSessionError(
                    str(admission["reason"] or "STT was refused due to resource pressure"),
                    str(admission["reason_code"] or "STT_RESOURCE_LIMIT"),
                )
            audio = await self.storage.save_upload(voice_session_id, upload)
            # A user stop can happen while a WebView upload is being validated.
            # Recheck the state under the same lock used by cancel before the
            # upload is allowed to reach STT.
            async with self._lock:
                self._require(voice_session_id, {"AUDIO_PROCESSING"})
                self._update(voice_session_id, "TRANSCRIBING", audio_duration_ms=audio.duration_ms, audio_format="wav-16k-mono-pcm")
                # The browser owns live level-meter rendering, while these
                # durable events describe the recording/STT boundary.  No
                # audio bytes or transcript text are persisted.
                emit_voice_event(voice_session_id, "RECORDING_STOPPED", {"duration_ms": audio.duration_ms})
                emit_voice_event(voice_session_id, "AUDIO_READY", {"duration_ms": audio.duration_ms, "sample_rate": audio.sample_rate})
                emit_voice_event(voice_session_id, "STT_LOADING", {"provider": settings["provider"], "model": settings["model_id"]})
            if not resource_coordinator.acquire_stt(voice_session_id):
                raise VoiceSessionError("STT resources are busy", "STT_RESOURCE_LIMIT")
            stt_reserved = True
            try:
                async with self._lock:
                    self._require(voice_session_id, {"TRANSCRIBING"})
                    emit_voice_event(voice_session_id, "STT_STARTED", {"provider": settings["provider"], "model": settings["model_id"]})
                result = await self.stt.transcribe(voice_session_id=voice_session_id, audio_path=audio.path, audio_sha256=audio.sha256, audio_duration_ms=audio.duration_ms)
            finally:
                resource_coordinator.release_stt(voice_session_id)
                stt_reserved = False
            # Do not allow a late model result to overwrite CANCEL_REQUESTED or
            # CANCELLED.  All state changes after an await are serialized here.
            async with self._lock:
                session = self._require(voice_session_id, {"TRANSCRIBING"})
                transcript_hash = hashlib.sha256(result.text.encode("utf-8")).hexdigest()
                auto_send_eligible = bool(session["auto_send"]) and result.auto_send_safe
                self._update(voice_session_id, "REVIEWING", stt_provider=result.provider, stt_model=result.model, transcription_text_hash=transcript_hash, transcription_duration_ms=result.transcription_ms)
                emit_voice_event(voice_session_id, "STT_COMPLETED", {"provider": result.provider, "model": result.model, "duration_ms": result.transcription_ms, "text_length": len(result.text)})
                emit_voice_event(voice_session_id, "MESSAGE_READY", {"text_length": len(result.text), "auto_send": auto_send_eligible})
                self.storage.delete(voice_session_id)
                response_session = self.get(voice_session_id)
                # Keep the persisted preference unchanged.  This response field
                # is the per-result authorization consumed by the renderer.
                response_session["auto_send"] = auto_send_eligible
                return {"session": response_session, "transcription": result.as_dict()}
        except (STTError, VoiceSessionError) as exc:
            outcome = await self._finish_processing_error(
                voice_session_id,
                exc.code,
                processing_started=processing_started,
            )
            if outcome == "cancelled":
                raise VoiceSessionError("Voice session was cancelled", "STT_ALREADY_CANCELLED") from exc
            raise VoiceSessionError("Voice transcription failed", exc.code) from exc
        except Exception as exc:
            outcome = await self._finish_processing_error(
                voice_session_id,
                "STT_TRANSCRIPTION_FAILED",
                processing_started=processing_started,
            )
            if outcome == "cancelled":
                raise VoiceSessionError("Voice session was cancelled", "STT_ALREADY_CANCELLED") from exc
            raise VoiceSessionError("Voice transcription failed", "STT_TRANSCRIPTION_FAILED") from exc
        finally:
            if stt_reserved:
                resource_coordinator.release_stt(voice_session_id)

    async def cancel(self, voice_session_id: str, *, reason: str = "user_cancelled") -> dict[str, Any]:
        """Cancel one voice session and its bound Agent task, if any.

        Agent cancellation happens before voice/STT/TTS teardown.  This keeps
        an ordinary message submitted from voice on the same authoritative
        cancellation path as a text task instead of merely closing its voice
        telemetry while the queued or running task continues.
        """
        async with self._lock:
            session = self._require(voice_session_id, None)
            if session["state"] in TERMINAL_STATES:
                return session
            if session["state"] not in ACTIVE_STATES:
                return session
            task_id = str(session["task_id"]) if session.get("task_id") else None
        agent_cancellation = await self._cancel_bound_agent_task(task_id) if task_id else None
        final, _ = await self._cancel_session_resources(
            voice_session_id,
            reason=reason,
            agent_cancellation=agent_cancellation,
        )
        return final

    async def cancel_for_task(self, task_id: str) -> int:
        candidates = rows("SELECT voice_session_id FROM voice_sessions WHERE task_id=? AND state IN ({})".format(",".join("?" for _ in ACTIVE_STATES)), (task_id, *ACTIVE_STATES))
        agent_cancellation = await self._cancel_bound_agent_task(task_id)
        for item in candidates:
            await self._cancel_session_resources(
                str(item["voice_session_id"]),
                reason="task_cancelled",
                agent_cancellation=agent_cancellation,
            )
        return len(candidates)

    async def _cancel_bound_agent_task(self, task_id: str) -> dict[str, Any]:
        """Cancel one Agent task and all of its active durable queue items."""
        from app.runtime.queue_service import cancel as cancel_queue_item
        from app.runtime.runner import cancel_task
        from app.runtime.task_state import FINAL_TASK_STATUSES, TaskStatus

        task_rows = rows("SELECT status FROM agent_tasks WHERE id=?", (task_id,))
        status_before = str(task_rows[0]["status"]) if task_rows else "missing"
        queue_before = rows(
            "SELECT id,status FROM conversation_queue_items "
            "WHERE task_id=? AND status IN ('pending','claimed') ORDER BY created_at,id",
            (task_id,),
        )
        queue_results: list[dict[str, Any]] = []
        for item in queue_before:
            item_id = str(item["id"])
            try:
                cancelled = cancel_queue_item(item_id)
                queue_results.append(
                    {
                        "id": item_id,
                        "status_before": str(item["status"]),
                        "status": str(cancelled.status),
                    }
                )
            except (KeyError, ValueError) as exc:
                current = rows(
                    "SELECT status FROM conversation_queue_items WHERE id=?",
                    (item_id,),
                )
                queue_results.append(
                    {
                        "id": item_id,
                        "status_before": str(item["status"]),
                        "status": str(current[0]["status"]) if current else "missing",
                        "error_code": type(exc).__name__,
                    }
                )

        runtime_result: dict[str, Any]
        try:
            runtime_result = dict(cancel_task(task_id))
        except Exception as exc:
            # A racing terminal transition can make the original cancel call
            # lose its compare-and-set.  Durable state below remains the
            # authority; only expose the exception type, never task content.
            runtime_result = {"status": "ERROR", "error_code": type(exc).__name__}

        terminal = {status.value for status in FINAL_TASK_STATUSES}
        deadline = time.monotonic() + self._TASK_CANCELLATION_SETTLE_SECONDS
        current_status = status_before
        active_queue: list[dict[str, Any]] = []
        while True:
            current = rows("SELECT status FROM agent_tasks WHERE id=?", (task_id,))
            current_status = str(current[0]["status"]) if current else "missing"
            active_queue = rows(
                "SELECT id,status FROM conversation_queue_items "
                "WHERE task_id=? AND status IN ('pending','claimed') ORDER BY created_at,id",
                (task_id,),
            )
            if current_status in terminal and not active_queue:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.01)

        task_found = current_status != "missing"
        active = task_found and current_status not in terminal
        return {
            "task_id": task_id,
            "status_before": status_before,
            "status": current_status,
            "cancelled": current_status == TaskStatus.CANCELLED.value,
            "active": active,
            "settled": task_found and not active and not active_queue,
            "interrupted": bool(runtime_result.get("interrupted")),
            "runtime_status": str(runtime_result.get("status") or "unknown"),
            "runtime_error_code": runtime_result.get("error_code"),
            "queue_items": queue_results,
            "queue_items_active": len(active_queue),
        }

    @staticmethod
    def _cancellation_has_unsettled_owner(
        *,
        task_id: str | None,
        agent_cancellation: dict[str, Any] | None,
        tts_result: dict[str, Any],
        stt_result: dict[str, Any],
        storage_result: dict[str, Any],
    ) -> bool:
        """Return whether a cancellation result is still only a request.

        A terminal Voice Session is a convergence claim.  In particular,
        callers must not infer completion from a successful Agent transition
        while an STT/TTS owner reports an unresolved request or throws.
        """
        agent_unsettled = bool(
            task_id
            and (
                agent_cancellation is None
                or bool(agent_cancellation.get("active"))
                or int(agent_cancellation.get("queue_items_active") or 0) > 0
                or not bool(agent_cancellation.get("settled"))
            )
        )
        tts_unsettled = (
            tts_result.get("status") not in {"CANCELLED", "NOT_APPLICABLE", "ALREADY_TERMINAL"}
            or tts_result.get("settled") is False
            or bool(tts_result.get("unresolved_requests"))
        )
        stt_unsettled = (
            stt_result.get("status") not in {"CANCELLED", "NOT_APPLICABLE", "ALREADY_TERMINAL"}
            or stt_result.get("settled") is False
        )
        storage_unsettled = storage_result.get("status") != "DELETED"
        return agent_unsettled or tts_unsettled or stt_unsettled or storage_unsettled

    async def _finalize_cancellation(
        self,
        voice_session_id: str,
        *,
        prior_state: str,
        task_id: str | None,
        reason: str,
        agent_cancellation: dict[str, Any] | None,
        tts_result: dict[str, Any],
        stt_result: dict[str, Any],
        storage_result: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Write CANCELLED only after every owned cleanup result settled."""
        if self._cancellation_has_unsettled_owner(
            task_id=task_id,
            agent_cancellation=agent_cancellation,
            tts_result=tts_result,
            stt_result=stt_result,
            storage_result=storage_result,
        ):
            return self.get(voice_session_id), False

        transitioned = False
        async with self._lock:
            current = self.get(voice_session_id)
            if current["state"] == "CANCEL_REQUESTED":
                self._update(voice_session_id, "CANCELLED", error_code=reason)
                if (
                    task_id
                    and agent_cancellation is not None
                    and agent_cancellation.get("status") == "cancelled"
                ):
                    emit_voice_event(
                        voice_session_id,
                        "AGENT_CANCELLED",
                        {"task_id": task_id, "status": "cancelled"},
                    )
                elif not task_id or prior_state in CAPTURE_ACTIVE_STATES:
                    emit_voice_event(voice_session_id, "STT_CANCELLED", {"reason": reason})
                emit_voice_event(voice_session_id, "VOICE_SESSION_COMPLETED", {"status": "CANCELLED"})
                transitioned = True
        if transitioned:
            resource_coordinator.release_voice_session(voice_session_id)
        return self.get(voice_session_id), transitioned

    async def _cancel_session_resources(
        self,
        voice_session_id: str,
        *,
        reason: str,
        agent_cancellation: dict[str, Any] | None,
        defer_terminal: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Converge one session after its Agent cancellation was attempted."""
        async with self._lock:
            session = self._require(voice_session_id, None)
            if session["state"] in TERMINAL_STATES or session["state"] not in ACTIVE_STATES:
                return session, {"status": "ALREADY_TERMINAL"}
            prior_state = str(session["state"])
            task_id = str(session["task_id"]) if session.get("task_id") else None
            self._update(voice_session_id, "CANCEL_REQUESTED")

        if task_id:
            self._clear_tts_dispatch_reservation(task_id)

        if task_id:
            try:
                tts_result = dict(await self.tts.interrupt(task_id=task_id))
            except Exception as exc:
                tts_result = {"status": "FAILED", "error_code": type(exc).__name__}
        elif prior_state in {"TTS_PLAYING", "CANCEL_REQUESTED"}:
            # A task-scoped request normally always has task_id.  If a
            # renderer-owned/orphaned TTS row lacks it, its global owner must
            # still settle before a session cancellation can be terminal.
            try:
                tts_result = dict(await self.tts.interrupt())
            except Exception as exc:
                tts_result = {"status": "FAILED", "error_code": type(exc).__name__}
        else:
            tts_result = {"status": "NOT_APPLICABLE"}

        # A bound Agent task has already left microphone/STT ownership.  A
        # later retry sees CANCEL_REQUESTED, so task_id keeps that retry from
        # spuriously cancelling an unrelated STT worker.
        if prior_state in CAPTURE_ACTIVE_STATES and not task_id:
            try:
                stt_result = dict(await self.stt.cancel(voice_session_id=voice_session_id))
            except Exception as exc:
                stt_result = {"status": "FAILED", "error_code": type(exc).__name__}
        else:
            stt_result = {"status": "NOT_APPLICABLE"}
        try:
            self.storage.delete(voice_session_id)
            storage_result = {"status": "DELETED"}
        except Exception as exc:
            storage_result = {"status": "FAILED", "error_code": type(exc).__name__}

        if defer_terminal or self._cancellation_has_unsettled_owner(
            task_id=task_id,
            agent_cancellation=agent_cancellation,
            tts_result=tts_result,
            stt_result=stt_result,
            storage_result=storage_result,
        ):
            # A terminal Voice state is a convergence claim, not merely an
            # acknowledgement.  Keep the session non-terminal while any
            # authoritative owner still reports active/failed cleanup.
            return self.get(voice_session_id), {
                "status": "CANCEL_REQUESTED",
                "agent": agent_cancellation or {"status": "UNKNOWN"},
                "tts": tts_result,
                "stt": stt_result,
                "storage": storage_result,
            }

        final, transitioned = await self._finalize_cancellation(
            voice_session_id,
            prior_state=prior_state,
            task_id=task_id,
            reason=reason,
            agent_cancellation=agent_cancellation,
            tts_result=tts_result,
            stt_result=stt_result,
            storage_result=storage_result,
        )
        return final, {
            "status": "CANCELLED" if transitioned else "ALREADY_TERMINAL",
            "agent": agent_cancellation or {"status": "NOT_APPLICABLE"},
            "tts": tts_result,
            "stt": stt_result,
            "storage": storage_result,
        }

    async def agent_started(self, task_id: str) -> int:
        """Reflect normal Agent execution without letting voice bypass it."""
        async with self._lock:
            candidates = rows(
                "SELECT voice_session_id FROM voice_sessions WHERE task_id=? AND state='QUEUED_FOR_AGENT'",
                (task_id,),
            )
            for item in candidates:
                voice_session_id = str(item["voice_session_id"])
                self._update(voice_session_id, "AGENT_RUNNING")
                emit_voice_event(voice_session_id, "AGENT_STARTED", {"task_id": task_id})
        return len(candidates)

    def reserve_tts_dispatch(self, task_id: str) -> int:
        """Hold a voice session while the renderer dispatches streamed TTS.

        The renderer owns audio playback, so its final ``/tts/speak`` request
        can legitimately arrive after the Agent's terminal event.  Reserve
        only for the configured automatic local TTS path and only when the
        task is actually bound to an active voice session.
        """
        if not task_id or task_id in self._tts_dispatch_reservations:
            return 0
        settings = getattr(self.tts, "settings", None)
        try:
            value = settings() if callable(settings) else {}
        except Exception:
            return 0
        if not bool(value.get("enabled")) or str(value.get("playback_mode") or "").upper() != "AUTO":
            return 0
        candidates = rows(
            "SELECT voice_session_id FROM voice_sessions WHERE task_id=? AND state IN ('QUEUED_FOR_AGENT','AGENT_RUNNING','TTS_PLAYING')",
            (task_id,),
        )
        if not candidates:
            return 0
        self._tts_dispatch_reservations.add(task_id)
        self._schedule_tts_dispatch_expiry(task_id)
        return len(candidates)

    async def tts_dispatch_finished(self, task_id: str) -> int:
        """Release the renderer dispatch reservation after its TTS queue ends.

        This operation is idempotent.  Actual TTS requests still own their
        normal ``_speak_intents`` lifecycle; a session becomes terminal only
        when both the renderer reservation and real playback are absent.
        """
        self._clear_tts_dispatch_reservation(task_id)
        release: list[str] = []
        async with self._lock:
            candidates = rows(
                "SELECT voice_session_id FROM voice_sessions WHERE task_id=? AND state IN ('QUEUED_FOR_AGENT','AGENT_RUNNING','TTS_PLAYING')",
                (task_id,),
            )
            if self._tts_has_pending(task_id):
                return len(candidates)
            for item in candidates:
                voice_session_id = str(item["voice_session_id"])
                if not self._agent_completed(voice_session_id):
                    continue
                self._update(voice_session_id, "COMPLETED")
                emit_voice_event(voice_session_id, "VOICE_SESSION_COMPLETED", {"status": "COMPLETED"})
                release.append(voice_session_id)
        for voice_session_id in release:
            resource_coordinator.release_voice_session(voice_session_id)
        return len(candidates)

    async def agent_finished(self, task_id: str, *, task_status: str) -> int:
        normalized_status = str(task_status).lower()
        terminal_state = "CANCELLED" if normalized_status == "cancelled" else ("FAILED" if normalized_status in {"failed", "blocked", "timed_out"} else "COMPLETED")
        event = {
            "COMPLETED": "AGENT_COMPLETED",
            "FAILED": "AGENT_FAILED",
            "CANCELLED": "AGENT_CANCELLED",
        }[terminal_state]
        # A failed/cancelled task must not leave an already-buffered reply
        # playing.  Do this before taking the voice lock: TTS calls back into
        # this manager to persist TTS_STOPPED, and lock inversion would make a
        # cancel race deadlock-prone.
        if terminal_state != "COMPLETED":
            self._clear_tts_dispatch_reservation(task_id)
            try:
                await self.tts.interrupt(task_id=task_id)
            except Exception:
                # The cancellation retry below is the authority for a Voice
                # Session already in CANCEL_REQUESTED.  Do not let a failed
                # best-effort interrupt turn that durable state into a false
                # terminal acknowledgement.
                pass
        release: list[str] = []
        cancellation_retries: list[str] = []
        async with self._lock:
            candidates = rows(
                "SELECT voice_session_id,state FROM voice_sessions WHERE task_id=? "
                "AND state IN ('QUEUED_FOR_AGENT','AGENT_RUNNING','TTS_PLAYING','CANCEL_REQUESTED')",
                (task_id,),
            )
            for item in candidates:
                voice_session_id = str(item["voice_session_id"])
                if item["state"] == "CANCEL_REQUESTED":
                    # A prior stop/cancel deliberately kept this Voice
                    # Session non-terminal because an owner had not settled.
                    # Re-run the authoritative teardown after the durable
                    # Agent result instead of claiming that Agent completion
                    # alone proves STT/TTS convergence.
                    if terminal_state != "CANCELLED":
                        emit_voice_event(voice_session_id, event, {"task_id": task_id, "status": normalized_status})
                    cancellation_retries.append(voice_session_id)
                    continue
                emit_voice_event(voice_session_id, event, {"task_id": task_id, "status": normalized_status})
                if terminal_state == "COMPLETED" and self._tts_has_pending(task_id):
                    # A Voice Session includes the task-scoped output audio.
                    # It must remain observable until TTS reports a terminal
                    # event, even though the Agent task itself is complete.
                    self._update(voice_session_id, "TTS_PLAYING")
                    continue
                self._update(voice_session_id, terminal_state, error_code=None if terminal_state == "COMPLETED" else f"agent_{normalized_status}")
                emit_voice_event(voice_session_id, "VOICE_SESSION_COMPLETED", {"status": terminal_state})
                release.append(voice_session_id)
        for voice_session_id in release:
            resource_coordinator.release_voice_session(voice_session_id)
        if cancellation_retries:
            agent_cancellation = {
                "task_id": task_id,
                "status": normalized_status,
                "active": False,
                "settled": True,
                "queue_items_active": 0,
            }
            for voice_session_id in cancellation_retries:
                await self._cancel_session_resources(
                    voice_session_id,
                    reason="global_stop",
                    agent_cancellation=agent_cancellation,
                )
        return len(candidates)

    async def tts_playback_started(self, *, task_id: str, request_id: str) -> int:
        """Reflect a task-scoped playback start in its Voice Session."""
        async with self._lock:
            candidates = rows(
                "SELECT voice_session_id FROM voice_sessions WHERE task_id=? AND state IN ('QUEUED_FOR_AGENT','AGENT_RUNNING','TTS_PLAYING')",
                (task_id,),
            )
            for item in candidates:
                voice_session_id = str(item["voice_session_id"])
                self._update(voice_session_id, "TTS_PLAYING")
                emit_voice_event(voice_session_id, "TTS_STARTED", {"task_id": task_id, "request_id": request_id})
        return len(candidates)

    async def tts_playback_completed(self, *, task_id: str, request_id: str) -> int:
        """Persist TTS completion and finish a fully completed Voice Session."""
        return await self._finish_tts_playback(task_id=task_id, request_id=request_id, completed=True)

    async def tts_playback_stopped(self, *, task_id: str, request_id: str) -> int:
        """Persist a user/global/task interruption without changing Agent truth."""
        return await self._finish_tts_playback(task_id=task_id, request_id=request_id, completed=False)

    async def _finish_tts_playback(self, *, task_id: str, request_id: str, completed: bool) -> int:
        event = "TTS_COMPLETED" if completed else "TTS_STOPPED"
        release: list[str] = []
        async with self._lock:
            candidates = rows(
                "SELECT voice_session_id,state FROM voice_sessions WHERE task_id=? AND state IN ('QUEUED_FOR_AGENT','AGENT_RUNNING','TTS_PLAYING','CANCEL_REQUESTED')",
                (task_id,),
            )
            still_pending = self._tts_has_pending(task_id)
            for item in candidates:
                voice_session_id = str(item["voice_session_id"])
                emit_voice_event(voice_session_id, event, {"task_id": task_id, "request_id": request_id})
                if item["state"] == "CANCEL_REQUESTED":
                    # Cancellation owns the terminal state; preserve its
                    # order while still reporting that playback was stopped.
                    continue
                if still_pending:
                    continue
                if self._agent_completed(voice_session_id):
                    self._update(voice_session_id, "COMPLETED")
                    emit_voice_event(voice_session_id, "VOICE_SESSION_COMPLETED", {"status": "COMPLETED"})
                    release.append(voice_session_id)
                elif item["state"] == "TTS_PLAYING":
                    # A streamed sentence can finish before the Agent does.
                    # Resume the Agent-visible state instead of claiming that
                    # the task or the Voice Session has completed.
                    self._update(voice_session_id, "AGENT_RUNNING")
        for voice_session_id in release:
            resource_coordinator.release_voice_session(voice_session_id)
        return len(candidates)

    async def stop(self, voice_session_id: str | None = None, *, reason: str = "global_stop") -> dict[str, Any]:
        """Stop one or every active voice workflow through authoritative owners.

        Bound Agent tasks and their pending/claimed queue items settle first.
        Only then are task-scoped TTS, STT, temporary audio, and Voice Session
        state stopped.  The aggregate reports unresolved work instead of
        inferring that an Agent was cancelled from the voice state alone.
        """
        async with self._lock:
            if voice_session_id:
                selected = [self._require(voice_session_id, None)]
                targets = [item for item in selected if str(item["state"]) in ACTIVE_STATES]
            else:
                targets = rows(
                    "SELECT * FROM voice_sessions WHERE state IN ({}) ORDER BY created_at,voice_session_id".format(
                        ",".join("?" for _ in ACTIVE_STATES)
                    ),
                    tuple(ACTIVE_STATES),
                )
                selected = list(targets)

        task_ids = list(
            dict.fromkeys(
                str(item["task_id"])
                for item in targets
                if item.get("task_id")
            )
        )
        task_results: dict[str, dict[str, Any]] = {}
        for task_id in task_ids:
            task_results[task_id] = await self._cancel_bound_agent_task(task_id)

        global_scope = voice_session_id is None
        session_results: list[dict[str, Any]] = []
        for item in targets:
            session_id = str(item["voice_session_id"])
            task_id = str(item["task_id"]) if item.get("task_id") else None
            final, actions = await self._cancel_session_resources(
                session_id,
                reason=reason,
                agent_cancellation=task_results.get(task_id) if task_id else None,
                # For a global stop, latch every selected session in
                # CANCEL_REQUESTED before interrupting unbound playback.  A
                # failed global TTS interrupt must not leave already-terminal
                # Voice rows behind.
                defer_terminal=global_scope,
            )
            session_results.append(
                {
                    "voice_session_id": session_id,
                    "task_id": task_id,
                    "state_before": str(item["state"]),
                    "state": str(final["state"]),
                    "actions": actions,
                }
            )

        # A global stop also clears orphaned/unbound playback.  Do this after
        # each selected session is CANCEL_REQUESTED so TTS callbacks cannot
        # race a stopped session back to AGENT_RUNNING or COMPLETED.
        global_tts: dict[str, Any] = {"status": "NOT_APPLICABLE"}
        if global_scope:
            try:
                global_tts = dict(await self.tts.interrupt())
            except Exception as exc:
                global_tts = {"status": "FAILED", "error_code": type(exc).__name__}

        global_tts_unsettled = (
            global_tts.get("status") not in {"CANCELLED", "NOT_APPLICABLE", "ALREADY_TERMINAL"}
            or global_tts.get("settled") is False
            or bool(global_tts.get("unresolved_requests"))
        )
        if global_scope and not global_tts_unsettled:
            # The first pass above performed every owner cleanup but deferred
            # the durable terminal write.  Finalize only those sessions whose
            # own Agent/STT/TTS/storage receipts also converged.
            for item in session_results:
                actions = item.get("actions")
                task_id = str(item["task_id"]) if item.get("task_id") else None
                if not isinstance(actions, dict) or item.get("state") != "CANCEL_REQUESTED":
                    continue
                tts_result = dict(actions.get("tts") or {})
                stt_result = dict(actions.get("stt") or {})
                storage_result = dict(actions.get("storage") or {})
                agent_cancellation = task_results.get(task_id) if task_id else None
                if self._cancellation_has_unsettled_owner(
                    task_id=task_id,
                    agent_cancellation=agent_cancellation,
                    tts_result=tts_result,
                    stt_result=stt_result,
                    storage_result=storage_result,
                ):
                    continue
                final, transitioned = await self._finalize_cancellation(
                    str(item["voice_session_id"]),
                    prior_state=str(item["state_before"]),
                    task_id=task_id,
                    reason=reason,
                    agent_cancellation=agent_cancellation,
                    tts_result=tts_result,
                    stt_result=stt_result,
                    storage_result=storage_result,
                )
                item["state"] = str(final["state"])
                if transitioned:
                    actions["status"] = "CANCELLED"
                elif item["state"] in TERMINAL_STATES:
                    actions["status"] = "ALREADY_TERMINAL"

        if voice_session_id and not targets:
            item = selected[0]
            session_results.append(
                {
                    "voice_session_id": str(item["voice_session_id"]),
                    "task_id": str(item["task_id"]) if item.get("task_id") else None,
                    "state_before": str(item["state"]),
                    "state": str(item["state"]),
                    "actions": {"status": "ALREADY_TERMINAL"},
                }
            )

        unresolved_tasks = any(
            bool(result.get("active")) or int(result.get("queue_items_active") or 0) > 0
            for result in task_results.values()
        )
        unresolved_sessions = any(item["state"] in ACTIVE_STATES for item in session_results)
        unresolved_resources = any(
            isinstance(actions := item.get("actions"), dict)
            and actions.get("status") == "CANCEL_REQUESTED"
            for item in session_results
        )
        queue_active_count = sum(
            int(result.get("queue_items_active") or 0)
            for result in task_results.values()
        )
        active_count = sum(
            item["state"] in ACTIVE_STATES for item in session_results
        ) + sum(bool(result.get("active")) for result in task_results.values())
        unresolved_count = (
            active_count
            + queue_active_count
            + sum(
                isinstance(actions := item.get("actions"), dict)
                and actions.get("status") == "CANCEL_REQUESTED"
                for item in session_results
            )
            + int(global_tts_unsettled)
            + len(global_tts.get("unresolved_requests") or [])
        )
        status = (
            "CANCEL_REQUESTED"
            if unresolved_tasks
            or unresolved_sessions
            or unresolved_resources
            or global_tts_unsettled
            else "CANCELLED"
        )
        return {
            "status": status,
            "scope": "session" if voice_session_id else "all",
            "requested_voice_session_id": voice_session_id,
            "sessions_targeted": len(targets),
            "sessions_cancelled": sum(
                item.get("state") == "CANCELLED"
                and item.get("state_before") in ACTIVE_STATES
                for item in session_results
            ),
            "sessions": session_results,
            "agent_tasks": list(task_results.values()),
            "tts": global_tts,
            "summary": {
                "target_count": len(targets),
                "active_count": active_count,
                "queue_active_count": queue_active_count,
                "unresolved_count": unresolved_count,
            },
        }

    async def stop_all(self) -> int:
        result = await self.stop()
        return int(result["sessions_targeted"])

    def bind_message_in_transaction(self, db: Any, *, voice_session_id: str, conversation_id: int, task_id: str, message_id: int) -> None:
        """Bind the reviewed transcript to the ordinary message in its DB transaction."""
        session = db.execute("SELECT conversation_id,state FROM voice_sessions WHERE voice_session_id=?", (voice_session_id,)).fetchone()
        if session is None:
            raise VoiceSessionError("Voice session not found", "VOICE_SESSION_NOT_FOUND")
        if str(session["state"]) != "REVIEWING" or int(session["conversation_id"]) != conversation_id:
            raise VoiceSessionError("Voice session is not ready for this conversation", "VOICE_SESSION_CONFLICT")
        db.execute(
            "UPDATE voice_sessions SET task_id=?,message_id=?,state='QUEUED_FOR_AGENT',updated_at=? WHERE voice_session_id=?",
            (task_id, message_id, now_iso(), voice_session_id),
        )

    def finish_message_binding(self, *, voice_session_id: str, task_id: str, message_id: int) -> None:
        emit_voice_event(voice_session_id, "MESSAGE_SENT", {"task_id": task_id, "message_id": message_id})
        # Queueing a normal user message is not a Voice Session completion.
        # Keep SSE open for AGENT_COMPLETED/FAILED/CANCELLED, but release the
        # microphone reservation now so Agent output TTS can begin.
        emit_voice_event(voice_session_id, "VOICE_INPUT_QUEUED", {"task_id": task_id, "message_id": message_id})
        resource_coordinator.release_voice_session(voice_session_id)

    def bind_message(self, *, voice_session_id: str, conversation_id: int, task_id: str, message_id: int) -> None:
        with connect() as db:
            self.bind_message_in_transaction(
                db,
                voice_session_id=voice_session_id,
                conversation_id=conversation_id,
                task_id=task_id,
                message_id=message_id,
            )
        self.finish_message_binding(voice_session_id=voice_session_id, task_id=task_id, message_id=message_id)

    def get(self, voice_session_id: str) -> dict[str, Any]:
        return self._require(voice_session_id, None)

    async def shutdown(self) -> None:
        await self.stop_all()
        for timeout in self._tts_dispatch_timeouts.values():
            timeout.cancel()
        self._tts_dispatch_timeouts.clear()
        self._tts_dispatch_reservations.clear()
        self.storage.cleanup_expired(0)

    def recover_interrupted_sessions(self) -> int:
        """Close stale microphone/STT sessions after an unclean sidecar exit.

        Agent task recovery remains owned by the existing task runtime.  A
        voice session is separate: it must never survive a process restart as
        an apparently recording/queued microphone session, and its temporary
        audio must be removed before accepting a new capture.
        """
        candidates = rows(
            "SELECT voice_session_id,state,task_id FROM voice_sessions WHERE state IN ({})".format(",".join("?" for _ in ACTIVE_STATES)),
            tuple(ACTIVE_STATES),
        )
        for item in candidates:
            voice_session_id = str(item["voice_session_id"])
            with connect() as db:
                db.execute(
                    "UPDATE voice_sessions SET state='CANCELLED',error_code='app_restarted',updated_at=? "
                    "WHERE voice_session_id=? AND state IN ({})".format(",".join("?" for _ in ACTIVE_STATES)),
                    (now_iso(), voice_session_id, *ACTIVE_STATES),
                )
            self.storage.delete(voice_session_id)
            prior_state = str(item["state"])
            task_id = str(item["task_id"]) if item.get("task_id") else None
            if prior_state == "TTS_PLAYING":
                emit_voice_event(voice_session_id, "TTS_STOPPED", {"task_id": task_id} if task_id else {})
            elif prior_state in {"QUEUED_FOR_AGENT", "AGENT_RUNNING"} and task_id:
                task = rows("SELECT status FROM agent_tasks WHERE id=?", (task_id,))
                if task and str(task[0]["status"]) == "cancelled":
                    emit_voice_event(voice_session_id, "AGENT_CANCELLED", {"task_id": task_id, "status": "cancelled"})
            else:
                emit_voice_event(voice_session_id, "STT_CANCELLED", {"reason": "app_restarted"})
            emit_voice_event(voice_session_id, "VOICE_SESSION_COMPLETED", {"status": "CANCELLED"})
            resource_coordinator.release_voice_session(voice_session_id)
        self.storage.cleanup_expired(0)
        return len(candidates)

    async def _finish_processing_error(self, voice_session_id: str, code: str, *, processing_started: bool) -> str:
        """Finish an STT failure without overwriting a concurrent cancellation."""
        if not processing_started:
            return "unstarted"
        outcome = "unstarted"
        async with self._lock:
            session = self.get(voice_session_id)
            state = str(session["state"])
            if state in CANCELLING_STATES:
                outcome = "cancelled"
            elif state in TERMINAL_STATES:
                outcome = state.lower()
            # A duplicate /complete after a successful transcription must not
            # turn REVIEWING into FAILED.  Only the real processing states own
            # this error path.
            elif state in {"AUDIO_PROCESSING", "TRANSCRIBING"}:
                self._update(voice_session_id, "FAILED", error_code=code)
                emit_voice_event(voice_session_id, "STT_FAILED", {"code": code})
                emit_voice_event(voice_session_id, "VOICE_SESSION_COMPLETED", {"status": "FAILED"})
                outcome = "failed"
        if outcome == "cancelled":
            # The cancel caller also deletes this directory, but it may still
            # be waiting on a native worker.  Deletion is idempotent and keeps
            # the temporary-audio guarantee true in that short race window.
            self.storage.delete(voice_session_id)
            return outcome
        if outcome != "failed":
            return outcome
        self.storage.delete(voice_session_id)
        resource_coordinator.release_voice_session(voice_session_id)
        return "failed"

    def _tts_has_pending(self, task_id: str) -> bool:
        probe = getattr(self.tts, "has_pending_for_task", None)
        tts_pending = bool(probe(task_id)) if callable(probe) else False
        return task_id in self._tts_dispatch_reservations or tts_pending

    def _clear_tts_dispatch_reservation(self, task_id: str) -> None:
        self._tts_dispatch_reservations.discard(task_id)
        timeout = self._tts_dispatch_timeouts.pop(task_id, None)
        if timeout is not None:
            timeout.cancel()

    def _schedule_tts_dispatch_expiry(self, task_id: str) -> None:
        timeout = self._tts_dispatch_timeouts.pop(task_id, None)
        if timeout is not None:
            timeout.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._tts_dispatch_timeouts[task_id] = loop.call_later(
            self._TTS_DISPATCH_GRACE_SECONDS,
            self._expire_tts_dispatch_reservation,
            task_id,
        )

    def _expire_tts_dispatch_reservation(self, task_id: str) -> None:
        self._tts_dispatch_timeouts.pop(task_id, None)
        if task_id not in self._tts_dispatch_reservations:
            return
        try:
            asyncio.get_running_loop().create_task(self.tts_dispatch_finished(task_id))
        except RuntimeError:
            # The event loop is already shutting down; a fresh server start
            # cancels any stale durable voice session during recovery.
            self._tts_dispatch_reservations.discard(task_id)

    def _stt_event_settings(self) -> dict[str, str]:
        """Return non-sensitive STT correlation metadata for voice events."""
        settings = getattr(self.stt, "settings", None)
        value = settings() if callable(settings) else {}
        return {
            "provider": str(value.get("provider") or "faster_whisper"),
            "model_id": str(value.get("model_id") or DEFAULT_STT_MODEL_ID),
            # Device choice is configuration metadata, not captured audio or
            # transcript content.  It determines whether STT admission also
            # needs the optional VRAM safety check.
            "device": str(value.get("device") or "cpu"),
        }

    @staticmethod
    def _agent_completed(voice_session_id: str) -> bool:
        """Use the durable event order, not a transient runtime flag.

        A TTS sentence may finish before the Agent's final task record is
        committed.  The Agent completion event is the point at which a voice
        session is allowed to become terminal.
        """
        return bool(
            rows(
                "SELECT 1 FROM voice_event_records WHERE voice_session_id=? AND event='AGENT_COMPLETED' LIMIT 1",
                (voice_session_id,),
            )
        )

    def _require(self, voice_session_id: str, allowed: set[str] | None) -> dict[str, Any]:
        records = rows("SELECT * FROM voice_sessions WHERE voice_session_id=?", (voice_session_id,))
        if not records:
            raise VoiceSessionError("Voice session not found", "VOICE_SESSION_NOT_FOUND")
        session = records[0]
        if allowed is not None and session["state"] not in allowed:
            raise VoiceSessionError(f"Voice session is not ready for this action ({session['state']})", "VOICE_SESSION_CONFLICT")
        return session

    def _update(self, voice_session_id: str, state: str, **fields: Any) -> None:
        assignments = ["state=?", "updated_at=?"]
        values: list[Any] = [state, now_iso()]
        for key in ("microphone_device_id", "recording_started_at", "recording_ended_at", "audio_duration_ms", "audio_format", "stt_provider", "stt_model", "transcription_text_hash", "transcription_duration_ms", "error_code"):
            if key in fields:
                assignments.append(f"{key}=?")
                values.append(fields[key])
        values.append(voice_session_id)
        with connect() as db:
            db.execute(f"UPDATE voice_sessions SET {','.join(assignments)} WHERE voice_session_id=?", tuple(values))


voice_session_manager = VoiceSessionManager()
