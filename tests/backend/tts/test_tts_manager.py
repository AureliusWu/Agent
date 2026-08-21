from __future__ import annotations

import asyncio
import os
import uuid
import wave
from pathlib import Path
import pytest

from app.tts.cache import AudioCache
from app.tts.manager import TTSManager, TTSManagerError, create_request
from app.tts.providers.base import TTSProvider, TTSProviderError
from app import database as database_module
from app.database import connect, now_iso, rows, tts_idempotency_digest
from app.local_runtime.resource_coordinator import ResourceCoordinator
from app.stt.temporary_storage import TemporaryAudioStore
from app.voice.events import voice_events
from app.voice.session_manager import VoiceSessionManager


class FakeUnavailable(TTSProvider):
    id = "melotts"
    name = "fake unavailable"
    version = "1"
    device = "cpu"
    async def health_check(self): return {"provider": self.id, "status": "unavailable"}
    async def list_voices(self): return []
    async def synthesize(self, request, output): raise TTSProviderError("missing", "TTS_MODEL_MISSING")
    async def cancel(self, request_id): return False
    async def unload(self): return {"status": "unloaded"}
    def get_status(self): return {"provider": self.id, "active_requests": []}
    def get_metrics(self): return {}


class FakeWindows(TTSProvider):
    id = "windows"
    name = "fake windows"
    version = "1"
    device = "cpu"
    calls = 0
    last_request = None
    async def health_check(self): return {"provider": self.id, "status": "ok"}
    async def list_voices(self): return [{"name": "zh", "culture": "zh-CN"}]
    async def synthesize(self, request, output):
        self.calls += 1
        self.last_request = request
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000); handle.writeframes(b"\x00\x00" * 2400)
        return {"duration_ms": 100, "sample_rate": 24000, "synthesis_ms": 5.0}
    async def cancel(self, request_id): return True
    async def unload(self): return {"status": "unloaded"}
    def get_status(self): return {"provider": self.id, "active_requests": []}
    def get_metrics(self): return {"calls": self.calls}


class SlowWindows(FakeWindows):
    """Provider double that leaves synthesis in flight until the test releases it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def synthesize(self, request, output):
        self.started.set()
        await self.release.wait()
        return await super().synthesize(request, output)


class StubbornWindows(FakeWindows):
    """Cancellation double that proves Stop cannot claim early convergence."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active: set[str] = set()

    async def synthesize(self, request, output):
        self.active.add(request.request_id)
        self.started.set()
        try:
            while not self.release.is_set():
                try:
                    await asyncio.wait_for(self.release.wait(), timeout=0.05)
                except asyncio.CancelledError:
                    # Simulate a native provider that has not acknowledged the
                    # first task cancellation yet.
                    continue
                except asyncio.TimeoutError:
                    continue
            return await super().synthesize(request, output)
        finally:
            self.active.discard(request.request_id)

    async def cancel(self, request_id):
        return False

    def get_status(self):
        return {"provider": self.id, "active_requests": sorted(self.active)}


def manager(tmp_path: Path) -> TTSManager:
    target = TTSManager(AudioCache(tmp_path / "cache"))
    target.temp = tmp_path / "temp"; target.temp.mkdir()
    target.providers = {"melotts": FakeUnavailable(), "windows": FakeWindows()}
    target.update_settings({"provider": "melotts", "fallback_provider": "windows", "allow_fallback": True, "cache_enabled": True})
    return target


def _prepared_voice_session(tmp_path: Path, target: TTSManager) -> tuple[VoiceSessionManager, str, str]:
    """Create an ordinary queued voice message linked to a task and TTS."""
    stamp = now_iso()
    task_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    with connect() as db:
        conversation_id = int(
            db.execute(
                "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("tts lifecycle", str(tmp_path), "ask", stamp, stamp),
            ).lastrowid
        )
        db.execute(
            "INSERT INTO voice_sessions(voice_session_id,conversation_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
            (session_id, conversation_id, "REVIEWING", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "voice reply", stamp, stamp),
        )
        message_id = int(
            db.execute(
                "INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
                (conversation_id, "user", "voice input", task_id, stamp),
            ).lastrowid
        )
    voice = VoiceSessionManager(stt=object(), tts=target, storage=TemporaryAudioStore(tmp_path / "voice-tmp"))
    voice.bind_message(
        voice_session_id=session_id,
        conversation_id=conversation_id,
        task_id=task_id,
        message_id=message_id,
    )
    return voice, session_id, task_id


def _bound_voice_session(tmp_path: Path, target: TTSManager) -> tuple[VoiceSessionManager, str, str]:
    voice, session_id, task_id = _prepared_voice_session(tmp_path, target)
    assert asyncio.run(voice.agent_started(task_id)) == 1
    return voice, session_id, task_id


async def _bound_voice_session_async(tmp_path: Path, target: TTSManager) -> tuple[VoiceSessionManager, str, str]:
    voice, session_id, task_id = _prepared_voice_session(tmp_path, target)
    assert await voice.agent_started(task_id) == 1
    return voice, session_id, task_id


def test_fallback_cache_queue_and_playback(tmp_path: Path) -> None:
    target = manager(tmp_path)
    first = asyncio.run(target.synthesize(create_request({"request_id":"one","idempotency_key":"one","text":"系统提示。","cache":True})))
    second = asyncio.run(target.synthesize(create_request({"request_id":"two","idempotency_key":"two","text":"系统提示。","cache":True})))
    assert first["provider"] == "windows" and first["cached"] is True
    assert second["cached"] is True and second["synthesis_ms"] == 0
    assert target.providers["windows"].calls == 1
    queued = asyncio.run(target.speak(create_request({"request_id":"three","idempotency_key":"three","task_id":"task","message_id":"message","text":"播放内容。","cache":False})))
    assert queued["status"] == "QUEUED"
    assert asyncio.run(target.playback_started("three"))["status"] == "PLAYING"
    assert asyncio.run(target.playback_finished("three"))["status"] == "COMPLETED"


def test_completed_task_scoped_noncache_playback_deletes_its_wav_and_converges_queue(tmp_path: Path) -> None:
    """The normal renderer acknowledgement path owns non-cache WAV cleanup."""

    target = manager(tmp_path)
    request = create_request(
        {
            "request_id": "complete-and-delete",
            "idempotency_key": "complete-and-delete",
            "task_id": "a20-isolated-task",
            "message_id": "a20-isolated-message",
            "text": "\u672c\u5730\u8bed\u97f3\u64ad\u653e\u5b8c\u6210\u540e\u5e94\u6e05\u7406\u4e34\u65f6\u97f3\u9891\u3002",
            "cache": False,
        }
    )

    queued = asyncio.run(target.speak(request))
    temporary = target.audio_path(request.request_id)
    assert queued["status"] == "QUEUED"
    assert temporary.is_file()
    assert target.queue()[0]["request_id"] == request.request_id
    assert target._speak_intents == {request.request_id: request.task_id}

    assert asyncio.run(target.playback_started(request.request_id))["status"] == "PLAYING"
    assert asyncio.run(target.playback_finished(request.request_id))["status"] == "COMPLETED"

    assert not temporary.exists()
    assert target.queue() == []
    assert target.status()["status"] == "IDLE"
    assert target._speak_intents == {}
    assert target._audio_paths == {}
    with pytest.raises(TTSManagerError) as raised:
        target.audio_path(request.request_id)
    assert raised.value.code == "TTS_CACHE_UNAVAILABLE"


def test_sensitive_text_is_never_cached(tmp_path: Path) -> None:
    target = manager(tmp_path)
    result = asyncio.run(target.synthesize(create_request({"request_id":"sensitive","idempotency_key":"sensitive","text":"正常提示。password: hidden-value","cache":True})))
    assert result["cached"] is False
    assert target.cache_list() == []


def test_idempotency_key_is_hashed_before_database_persistence(tmp_path: Path) -> None:
    target = manager(tmp_path)
    plaintext = "task:message:password-secret-should-not-persist"
    request = create_request(
        {
            "request_id": "hashed-idempotency",
            "idempotency_key": plaintext,
            "text": "ordinary local notification",
            "cache": False,
        }
    )

    first = asyncio.run(target.synthesize(request))
    duplicate = asyncio.run(target.synthesize(request))

    persisted = rows(
        "SELECT idempotency_key FROM tts_requests WHERE request_id=?",
        (request.request_id,),
    )[0]["idempotency_key"]
    assert persisted == tts_idempotency_digest(plaintext)
    assert plaintext not in persisted
    assert duplicate["request_id"] == first["request_id"]
    marker = plaintext.encode("utf-8")
    database = Path(database_module.settings.database_path)
    for candidate in (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    ):
        if candidate.exists():
            assert marker not in candidate.read_bytes(), candidate.name


def test_orphaned_sensitive_temporary_audio_is_removed_on_next_start(tmp_path: Path) -> None:
    target = manager(tmp_path)
    result = asyncio.run(
        target.synthesize(
            create_request(
                {
                    "request_id": "sensitive-orphan",
                    "idempotency_key": "sensitive-orphan",
                    "text": "这是需要清理的临时提示。password: must-not-remain-on-disk",
                    "cache": True,
                }
            )
        )
    )
    assert result["cached"] is False
    temporary = target.audio_path("sensitive-orphan")
    assert temporary.is_file()

    # A new sidecar has no in-memory ownership map for a previous renderer.
    target._audio_paths.clear()
    assert target.cleanup_orphaned_temporary_audio() == 1
    assert not temporary.exists()
    assert target.cache_list() == []


def test_automatic_speak_inherits_persisted_voice_speed_volume_and_sample_rate(tmp_path: Path) -> None:
    target = manager(tmp_path)
    target.update_settings(
        {
            "provider": "windows",
            "fallback_provider": "windows",
            "voice": "zh-CN-Test",
            "speed": 1.3,
            "volume": 0.4,
            "sample_rate": 16000,
        }
    )

    result = asyncio.run(
        target.speak(
            target.request_from_payload(
                {
                    "request_id": "configured-auto",
                    "idempotency_key": "configured-auto",
                    "task_id": "task",
                    "message_id": "message",
                    "text": "使用已保存配置。",
                    "cache": False,
                }
            )
        )
    )

    request = target.providers["windows"].last_request
    assert result["status"] == "QUEUED"
    assert request.voice == "zh-CN-Test"
    assert request.speed == 1.3
    assert request.volume == 0.4
    assert request.sample_rate == 16000


def test_ttl_reaper_clears_renderer_orphaned_task_audio_and_preserves_cache(tmp_path: Path) -> None:
    """A live sidecar must recover from a vanished renderer without new TTS."""
    async def scenario() -> None:
        target = manager(tmp_path)
        voice, session_id, task_id = await _bound_voice_session_async(tmp_path, target)
        queued = await target.speak(
            create_request(
                {
                    "request_id": "renderer-orphan",
                    "idempotency_key": "renderer-orphan",
                    "task_id": task_id,
                    "message_id": task_id,
                    "text": "renderer may disappear before acknowledgement",
                    "cache": False,
                }
            )
        )
        assert queued["status"] == "QUEUED"
        await voice.agent_finished(task_id, task_status="completed")
        assert voice.get(session_id)["state"] == "TTS_PLAYING"

        temporary = target.audio_path("renderer-orphan")
        cached = await target.synthesize(
            create_request(
                {
                    "request_id": "cache-survives-ttl",
                    "idempotency_key": "cache-survives-ttl",
                    "text": "ordinary cache entry",
                    "cache": True,
                }
            )
        )
        cache_path = target.audio_path(str(cached["request_id"]))
        assert cache_path.parent == target.cache.root

        expired_at = 1_700_000_000.0
        os.utime(temporary, (expired_at, expired_at))
        removed = await target.reap_expired_temporary_audio(
            now=expired_at + target.temporary_audio_ttl_seconds + 1
        )

        assert removed == 1
        assert not temporary.exists()
        assert cache_path.is_file()
        assert target.cache_list()
        assert target.queue() == []
        assert target._speak_intents == {}
        record = rows(
            "SELECT status,error_code FROM tts_requests WHERE request_id=?",
            ("renderer-orphan",),
        )[0]
        assert record == {
            "status": "CANCELLED",
            "error_code": "TTS_TEMPORARY_AUDIO_EXPIRED",
        }
        assert voice.get(session_id)["state"] == "COMPLETED"
        assert [event["event"] for event in target.events_after(0)][-1] == "tts.temporary_audio.expired"

    asyncio.run(scenario())


def test_stop_is_idempotent_and_clears_queue(tmp_path: Path) -> None:
    target = manager(tmp_path)
    asyncio.run(target.speak(create_request({"request_id":"stop-me","idempotency_key":"stop-me","task_id":"task","text":"停止测试。","cache":False})))
    first = asyncio.run(target.stop(task_id="task"))
    second = asyncio.run(target.stop(task_id="task"))
    assert first["cleared_queue"] == 1
    assert second["cleared_queue"] == 0
    assert target.status()["status"] == "IDLE"
    with pytest.raises(Exception):
        target.audio_path("stop-me")


def test_recording_start_during_synthesis_cancels_tts_before_enqueue(monkeypatch, tmp_path: Path) -> None:
    """A slow provider must not queue audio after the microphone becomes active."""
    coordinator = ResourceCoordinator()
    monkeypatch.setattr("app.tts.manager.resource_coordinator", coordinator)

    async def scenario() -> None:
        target = manager(tmp_path)
        slow = SlowWindows()
        target.providers = {"melotts": FakeUnavailable(), "windows": slow}
        request = create_request(
            {
                "request_id": "half-duplex-race",
                "idempotency_key": "half-duplex-race",
                "task_id": "voice-task",
                "message_id": "voice-message",
                "text": "录音开始后不应播放这句。password: temporary-only",
                "cache": False,
            }
        )

        speaking = asyncio.create_task(target.speak(request))
        await asyncio.wait_for(slow.started.wait(), timeout=1)
        assert coordinator.acquire_voice_session("recording-session") is True
        slow.release.set()

        try:
            with pytest.raises(TTSManagerError) as exc_info:
                await speaking
            assert exc_info.value.code == "TTS_INTERRUPTED_BY_VOICE_INPUT"
        finally:
            coordinator.release_voice_session("recording-session")

        assert target.queue() == []
        assert target.status()["queue_length"] == 0
        assert target._speak_intents == {}
        assert target._audio_paths == {}
        assert list(target.temp.glob("*.wav")) == []
        record = rows("SELECT status,error_code FROM tts_requests WHERE request_id=?", (request.request_id,))[0]
        assert record == {"status": "CANCELLED", "error_code": "TTS_INTERRUPTED_BY_VOICE_INPUT"}
        assert [event["event"] for event in target.events_after(0)] == [
            "tts.synthesis.started",
            "tts.synthesis.ready",
        ]

    asyncio.run(scenario())


def test_stop_does_not_claim_cancelled_while_provider_request_is_still_active(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[dict, dict]:
        target = manager(tmp_path)
        stubborn = StubbornWindows()
        target.providers = {"windows": stubborn}
        target.update_settings(
            {"provider": "windows", "fallback_provider": "windows", "allow_fallback": False}
        )
        request = create_request(
            {
                "request_id": "stubborn-request",
                "idempotency_key": "stubborn-request",
                "text": "受控停止测试。",
                "cache": False,
            }
        )
        synthesis = asyncio.create_task(target.synthesize(request))
        await stubborn.started.wait()
        first = await target.stop()
        assert first["status"] == "CANCEL_REQUESTED"
        assert first["settled"] is False
        assert first["unresolved_requests"] == [request.request_id]
        stubborn.release.set()
        await asyncio.gather(synthesis, return_exceptions=True)
        second = await target.stop()
        return first, second

    first, second = asyncio.run(scenario())
    assert first["status"] == "CANCEL_REQUESTED"
    assert second["status"] == "CANCELLED"
    assert second["settled"] is True


def test_voice_session_waits_for_task_scoped_tts_before_completion(tmp_path: Path) -> None:
    target = manager(tmp_path)
    voice, session_id, task_id = _bound_voice_session(tmp_path, target)

    queued = asyncio.run(
        target.speak(
            create_request(
                {
                    "request_id": "voice-reply",
                    "idempotency_key": "voice-reply",
                    "task_id": task_id,
                    "message_id": task_id,
                    "text": "语音回复。",
                    "cache": False,
                }
            )
        )
    )
    assert queued["status"] == "QUEUED"
    assert asyncio.run(voice.agent_finished(task_id, task_status="completed")) == 1
    assert voice.get(session_id)["state"] == "TTS_PLAYING"
    assert "VOICE_SESSION_COMPLETED" not in [event["event"] for event in voice_events(session_id)]

    assert asyncio.run(target.playback_started("voice-reply"))["status"] == "PLAYING"
    assert asyncio.run(target.playback_finished("voice-reply"))["status"] == "COMPLETED"

    assert voice.get(session_id)["state"] == "COMPLETED"
    assert [event["event"] for event in voice_events(session_id)] == [
        "MESSAGE_SENT",
        "VOICE_INPUT_QUEUED",
        "AGENT_STARTED",
        "AGENT_COMPLETED",
        "TTS_STARTED",
        "TTS_COMPLETED",
        "VOICE_SESSION_COMPLETED",
    ]


def test_task_scoped_tts_interrupt_converges_voice_session_without_rewriting_agent(tmp_path: Path) -> None:
    target = manager(tmp_path)
    voice, session_id, task_id = _bound_voice_session(tmp_path, target)

    asyncio.run(
        target.speak(
            create_request(
                {
                    "request_id": "interrupt-reply",
                    "idempotency_key": "interrupt-reply",
                    "task_id": task_id,
                    "message_id": task_id,
                    "text": "将被打断。",
                    "cache": False,
                }
            )
        )
    )
    asyncio.run(voice.agent_finished(task_id, task_status="completed"))
    asyncio.run(target.playback_started("interrupt-reply"))
    interrupted = asyncio.run(target.interrupt(task_id=task_id))

    assert interrupted["cleared_queue"] == 1
    assert voice.get(session_id)["state"] == "COMPLETED"
    assert [event["event"] for event in voice_events(session_id)][-3:] == [
        "TTS_STARTED",
        "TTS_STOPPED",
        "VOICE_SESSION_COMPLETED",
    ]


def test_agent_failure_stops_queued_tts_and_finishes_voice_as_failed(tmp_path: Path) -> None:
    target = manager(tmp_path)
    voice, session_id, task_id = _bound_voice_session(tmp_path, target)

    asyncio.run(
        target.speak(
            create_request(
                {
                    "request_id": "failed-reply",
                    "idempotency_key": "failed-reply",
                    "task_id": task_id,
                    "message_id": task_id,
                    "text": "不会播放。",
                    "cache": False,
                }
            )
        )
    )
    asyncio.run(voice.agent_finished(task_id, task_status="failed"))

    assert target.status()["queue_length"] == 0
    assert voice.get(session_id)["state"] == "FAILED"
    assert [event["event"] for event in voice_events(session_id)][-3:] == [
        "TTS_STOPPED",
        "AGENT_FAILED",
        "VOICE_SESSION_COMPLETED",
    ]


def test_voice_cancel_stops_playback_before_the_cancel_terminal_event(tmp_path: Path) -> None:
    target = manager(tmp_path)
    voice, session_id, task_id = _bound_voice_session(tmp_path, target)

    asyncio.run(
        target.speak(
            create_request(
                {
                    "request_id": "cancel-reply",
                    "idempotency_key": "cancel-reply",
                    "task_id": task_id,
                    "message_id": task_id,
                    "text": "取消时必须停止。",
                    "cache": False,
                }
            )
        )
    )
    asyncio.run(target.playback_started("cancel-reply"))
    cancelled = asyncio.run(voice.cancel(session_id, reason="task_cancelled"))

    assert cancelled["state"] == "CANCELLED"
    assert target.status()["queue_length"] == 0
    assert [event["event"] for event in voice_events(session_id)][-4:] == [
        "TTS_STARTED",
        "TTS_STOPPED",
        "AGENT_CANCELLED",
        "VOICE_SESSION_COMPLETED",
    ]


def test_cache_key_changes_with_voice_and_speed() -> None:
    base = {"provider":"windows","model_version":"1","voice":"a","normalized_text":"text","speed":1.0,"sample_rate":24000}
    first = AudioCache.key(**base)
    assert first != AudioCache.key(**{**base, "voice":"b"})
    assert first != AudioCache.key(**{**base, "speed":1.2})
