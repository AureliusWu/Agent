from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.database import connect, now_iso, rows
from app.local_runtime.resource_coordinator import ResourceCoordinator
from app.main import app
from app.runtime.task_runtime import _create_pending_task
from app.schemas import ChatRequest
from app.stt.schemas import DEFAULT_STT_MODEL_ID, TranscriptionResult
from app.stt.temporary_storage import TemporaryAudioStore
from app.voice.events import emit_voice_event, voice_events
from app.voice.session_manager import VoiceSessionError, VoiceSessionManager


@pytest.fixture(autouse=True)
def _voice_tests_inject_resource_headroom(monkeypatch):
    """Keep lifecycle tests about voice state, not ambient machine pressure."""
    monkeypatch.setattr(
        "app.local_runtime.resource_coordinator._memory",
        lambda: (16 * 1024 * 1024 * 1024, 8 * 1024 * 1024 * 1024),
    )
    monkeypatch.setattr(
        "app.local_runtime.resource_coordinator._gpu",
        lambda: (6 * 1024 * 1024 * 1024, 4 * 1024 * 1024 * 1024),
    )


class _NoopTTS:
    def __init__(self) -> None:
        self.interrupt_calls = 0

    async def interrupt(self, *, task_id: str | None = None) -> dict[str, object]:
        self.interrupt_calls += 1
        return {"status": "CANCELLED", "task_id": task_id}


class _AutoTTS(_NoopTTS):
    def settings(self) -> dict[str, object]:
        return {"enabled": True, "playback_mode": "AUTO"}

    def has_pending_for_task(self, _task_id: str) -> bool:
        return False


class _NoopSTT:
    def __init__(self, store: TemporaryAudioStore) -> None:
        self.audio_store = store

    async def cancel(self, **_: object) -> dict[str, object]:
        return {"status": "CANCELLED", "cancelled": 0, "settled": True}


class _TrackingTTS(_NoopTTS):
    def __init__(self) -> None:
        super().__init__()
        self.task_ids: list[str | None] = []

    async def interrupt(self, *, task_id: str | None = None) -> dict[str, object]:
        self.interrupt_calls += 1
        self.task_ids.append(task_id)
        return {"status": "CANCELLED", "task_id": task_id, "cleared_queue": 1}


class _TrackingSTT(_NoopSTT):
    def __init__(self, store: TemporaryAudioStore) -> None:
        super().__init__(store)
        self.voice_session_ids: list[str] = []

    async def cancel(self, **kwargs: object) -> dict[str, object]:
        voice_session_id = str(kwargs.get("voice_session_id") or "")
        self.voice_session_ids.append(voice_session_id)
        return {"status": "CANCELLED", "cancelled": 1, "settled": True}


class _UnsettledTTS(_TrackingTTS):
    async def interrupt(self, *, task_id: str | None = None) -> dict[str, object]:
        self.interrupt_calls += 1
        self.task_ids.append(task_id)
        return {
            "status": "CANCEL_REQUESTED",
            "settled": False,
            "unresolved_requests": ["still-running"],
        }


class _UnsettledSTT(_TrackingSTT):
    async def cancel(self, **kwargs: object) -> dict[str, object]:
        voice_session_id = str(kwargs.get("voice_session_id") or "")
        self.voice_session_ids.append(voice_session_id)
        return {"status": "CANCEL_REQUESTED", "settled": False, "cancelled": 1}


class _RaisingTTS(_TrackingTTS):
    async def interrupt(self, *, task_id: str | None = None) -> dict[str, object]:
        self.interrupt_calls += 1
        self.task_ids.append(task_id)
        raise RuntimeError("test-only TTS interrupt failure")


class _RaisingSTT(_TrackingSTT):
    async def cancel(self, **kwargs: object) -> dict[str, object]:
        self.voice_session_ids.append(str(kwargs.get("voice_session_id") or ""))
        raise RuntimeError("test-only STT interrupt failure")


class _GlobalRaisingTTS(_TrackingTTS):
    async def interrupt(self, *, task_id: str | None = None) -> dict[str, object]:
        self.interrupt_calls += 1
        self.task_ids.append(task_id)
        if task_id is None:
            raise RuntimeError("test-only global TTS interrupt failure")
        return {"status": "CANCELLED", "task_id": task_id, "settled": True}


class _LateResultSTT(_NoopSTT):
    def __init__(self, store: TemporaryAudioStore) -> None:
        super().__init__(store)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def settings(self) -> dict[str, str]:
        return {"provider": "faster_whisper", "model_id": "base"}

    async def transcribe(self, **_: object) -> TranscriptionResult:
        self.started.set()
        await self.release.wait()
        return TranscriptionResult(
            request_id=uuid.uuid4().hex,
            provider="faster_whisper",
            model="base",
            language="zh",
            text="late result must not be queued after cancellation",
            segments=[],
            duration_ms=400,
            transcription_ms=1.0,
            real_time_factor=0.01,
        )

    async def cancel(self, **_: object) -> dict[str, object]:
        self.release.set()
        return {"status": "CANCELLED", "cancelled": 1, "settled": True}


class _ImmediateResultSTT(_NoopSTT):
    def __init__(
        self,
        store: TemporaryAudioStore,
        *,
        confidence: float | None = None,
        reliable: bool = False,
        reliability_reason: str = "CONFIDENCE_UNAVAILABLE",
    ) -> None:
        super().__init__(store)
        self.confidence = confidence
        self.reliable = reliable
        self.reliability_reason = reliability_reason

    def settings(self) -> dict[str, str]:
        return {"provider": "faster_whisper", "model_id": "base"}

    async def transcribe(self, **_: object) -> TranscriptionResult:
        return TranscriptionResult(
            request_id=uuid.uuid4().hex,
            provider="faster_whisper",
            model="base",
            language="zh",
            text="local transcription",
            segments=[],
            duration_ms=400,
            transcription_ms=3.0,
            real_time_factor=0.01,
            confidence=self.confidence,
            reliable=self.reliable,
            reliability_reason=self.reliability_reason,
        )


def test_voice_event_metadata_uses_shared_small_default_when_stt_has_no_settings(
    tmp_path: Path,
) -> None:
    store = TemporaryAudioStore(tmp_path / "voice")
    manager = VoiceSessionManager(stt=_NoopSTT(store), tts=_NoopTTS(), storage=store)

    assert DEFAULT_STT_MODEL_ID == "small"
    assert manager._stt_event_settings() == {
        "provider": "faster_whisper",
        "model_id": "small",
        "device": "cpu",
    }


def _conversation_id(tmp_path: Path) -> int:
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("voice test", str(tmp_path), "ask", stamp, stamp),
        )
    return int(cursor.lastrowid)


def _reviewing_session(conversation_id: int) -> str:
    session_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO voice_sessions(voice_session_id,conversation_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
            (session_id, conversation_id, "REVIEWING", stamp, stamp),
        )
    return session_id


def _finish_test_session(session_id: str) -> None:
    """Keep this module's test-only session rows from affecting later tests."""
    with connect() as db:
        db.execute(
            "UPDATE voice_sessions SET state='COMPLETED',updated_at=? WHERE voice_session_id=?",
            (now_iso(), session_id),
        )


def _insert_bound_voice_session(
    *,
    conversation_id: int,
    task_status: str,
    voice_state: str,
) -> tuple[str, str]:
    task_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, task_status, "voice stop test", stamp, stamp),
        )
        db.execute(
            "INSERT INTO voice_sessions(voice_session_id,conversation_id,task_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (session_id, conversation_id, task_id, voice_state, stamp, stamp),
        )
    return session_id, task_id


def _wav_upload() -> UploadFile:
    import wave

    payload = io.BytesIO()
    with wave.open(payload, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x01\x00" * 6_400)
    return UploadFile(filename="recording.wav", file=io.BytesIO(payload.getvalue()), headers={"content-type": "audio/wav"})


def test_voice_session_refuses_ram_pressure_before_interrupting_existing_tts(tmp_path: Path, monkeypatch) -> None:
    coordinator = ResourceCoordinator(minimum_available_ram_bytes=100, minimum_free_vram_bytes=0)
    monkeypatch.setattr("app.voice.session_manager.resource_coordinator", coordinator)
    monkeypatch.setattr("app.local_runtime.resource_coordinator._memory", lambda: (1_000, 99))
    monkeypatch.setattr("app.local_runtime.resource_coordinator._gpu", lambda: (None, None))
    tts = _NoopTTS()
    manager = VoiceSessionManager(
        stt=_NoopSTT(TemporaryAudioStore(tmp_path / "voice-tmp")),
        tts=tts,
    )

    with pytest.raises(VoiceSessionError) as raised:
        asyncio.run(manager.create(conversation_id=_conversation_id(tmp_path)))

    assert raised.value.code == "RESOURCE_RAM_PRESSURE"
    assert tts.interrupt_calls == 0
    assert coordinator.recording_active is False


def test_voice_event_records_strip_audio_and_transcription_content(tmp_path: Path) -> None:
    conversation_id = _conversation_id(tmp_path)
    session_id = _reviewing_session(conversation_id)
    secret_text = "不得进入事件的转写原文"
    secret_audio = "data:audio/wav;base64,secret-audio"

    emitted = emit_voice_event(
        session_id,
        "STT_COMPLETED",
        {
            "text": secret_text,
            "transcript": secret_text,
            "audio": secret_audio,
            "audio_path": f"{chr(67)}:/private/recording.wav",
            "path": f"{chr(67)}:/private/recording.wav",
            "nested": {"transcript_text": secret_text, "recording": secret_audio},
            "unknown_raw_value": secret_text,
            "text_length": len(secret_text),
            "provider": "faster_whisper",
        },
    )

    assert emitted["payload"] == {"text_length": len(secret_text), "provider": "faster_whisper"}
    events = voice_events(session_id)
    assert events[0]["payload"] == emitted["payload"]
    persisted = rows("SELECT payload_json FROM voice_event_records WHERE voice_session_id=?", (session_id,))[0]["payload_json"]
    assert secret_text not in persisted
    assert secret_audio not in persisted
    assert "recording.wav" not in persisted
    assert json.loads(persisted) == emitted["payload"]
    _finish_test_session(session_id)


def test_voice_queue_event_is_not_a_completion_and_sse_ends_at_agent_terminal(tmp_path: Path) -> None:
    conversation_id = _conversation_id(tmp_path)
    session_id = _reviewing_session(conversation_id)
    task_id = uuid.uuid4().hex
    manager = VoiceSessionManager(stt=_NoopSTT(TemporaryAudioStore(tmp_path / "voice-tmp")), tts=_NoopTTS())
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "pending", "voice task", stamp, stamp),
        )
        message_id = int(
            db.execute(
                "INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
                (conversation_id, "user", "ordinary message", task_id, stamp),
            ).lastrowid
        )

    manager.bind_message(voice_session_id=session_id, conversation_id=conversation_id, task_id=task_id, message_id=message_id)

    queued_events = voice_events(session_id)
    assert [event["event"] for event in queued_events] == ["MESSAGE_SENT", "VOICE_INPUT_QUEUED"]
    assert manager.get(session_id)["state"] == "QUEUED_FOR_AGENT"
    assert not any(event["event"] == "VOICE_SESSION_COMPLETED" for event in queued_events)

    assert asyncio.run(manager.agent_started(task_id)) == 1
    assert asyncio.run(manager.agent_finished(task_id, task_status="completed")) == 1

    events = voice_events(session_id)
    assert [event["event"] for event in events] == [
        "MESSAGE_SENT",
        "VOICE_INPUT_QUEUED",
        "AGENT_STARTED",
        "AGENT_COMPLETED",
        "VOICE_SESSION_COMPLETED",
    ]
    assert manager.get(session_id)["state"] == "COMPLETED"
    with TestClient(app) as client:
        response = client.get(f"/api/voice/events?voice_session_id={session_id}")
    assert response.status_code == 200
    assert response.text.count("event: VOICE_SESSION_COMPLETED") == 1
    assert response.text.index("event: VOICE_INPUT_QUEUED") < response.text.index("event: VOICE_SESSION_COMPLETED")


def test_tts_dispatch_reservation_keeps_voice_session_open_for_final_stream_delta(tmp_path: Path) -> None:
    """The browser can issue its final TTS request after Agent completion."""

    async def scenario() -> tuple[str, list[str], VoiceSessionManager]:
        conversation_id = _conversation_id(tmp_path)
        session_id = _reviewing_session(conversation_id)
        task_id = uuid.uuid4().hex
        manager = VoiceSessionManager(stt=_NoopSTT(TemporaryAudioStore(tmp_path / "voice-tmp")), tts=_AutoTTS())
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (task_id, conversation_id, "pending", "voice task", stamp, stamp),
            )
            message_id = int(
                db.execute(
                    "INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
                    (conversation_id, "user", "ordinary message", task_id, stamp),
                ).lastrowid
            )
        manager.bind_message(voice_session_id=session_id, conversation_id=conversation_id, task_id=task_id, message_id=message_id)
        await manager.agent_started(task_id)
        assert manager.reserve_tts_dispatch(task_id) == 1
        await manager.agent_finished(task_id, task_status="completed")
        assert manager.get(session_id)["state"] == "TTS_PLAYING"
        assert [event["event"] for event in voice_events(session_id)][-1] == "AGENT_COMPLETED"
        await manager.tts_dispatch_finished(task_id)
        return session_id, [str(event["event"]) for event in voice_events(session_id)], manager

    session_id, events, manager = asyncio.run(scenario())

    assert manager.get(session_id)["state"] == "COMPLETED"
    assert events[-2:] == ["AGENT_COMPLETED", "VOICE_SESSION_COMPLETED"]


def test_cancel_race_cannot_queue_a_late_stt_result(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict[str, object], list[dict[str, object]], VoiceSessionManager]:
        store = TemporaryAudioStore(tmp_path / "voice-tmp")
        stt = _LateResultSTT(store)
        manager = VoiceSessionManager(stt=stt, tts=_NoopTTS(), storage=store)
        conversation_id = _conversation_id(tmp_path)
        created = await manager.create(conversation_id=conversation_id, device_id="test-mic")
        session_id = str(created["voice_session_id"])
        await manager.recording_started(session_id, device_id="test-mic")
        completing = asyncio.create_task(manager.complete(session_id, _wav_upload()))
        await asyncio.wait_for(stt.started.wait(), timeout=5)
        cancelled = await manager.cancel(session_id, reason="test_cancel")
        with pytest.raises(VoiceSessionError) as raised:
            await completing
        assert raised.value.code == "STT_ALREADY_CANCELLED"
        return cancelled, voice_events(session_id), manager

    cancelled, events, manager = asyncio.run(scenario())

    assert cancelled["state"] == "CANCELLED"
    assert manager.get(str(cancelled["voice_session_id"]))["state"] == "CANCELLED"
    names = [event["event"] for event in events]
    assert "STT_COMPLETED" not in names
    assert "MESSAGE_READY" not in names
    assert names[-2:] == ["STT_CANCELLED", "VOICE_SESSION_COMPLETED"]


def test_recording_and_stt_boundary_events_are_ordered_without_raw_content(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, list[dict[str, object]]]:
        store = TemporaryAudioStore(tmp_path / "voice-tmp")
        manager = VoiceSessionManager(stt=_ImmediateResultSTT(store), tts=_NoopTTS(), storage=store)
        created = await manager.create(conversation_id=_conversation_id(tmp_path), device_id="test-mic")
        session_id = str(created["voice_session_id"])
        await manager.recording_started(session_id, device_id="test-mic")
        await manager.recording_level(session_id, level=0.347)
        # The durable stream is intentionally capped at one event per second
        # even when the UI meter updates every animation frame.
        await manager.recording_level(session_id, level=0.999)
        result = await manager.complete(session_id, _wav_upload())
        assert result["session"]["state"] == "REVIEWING"
        events = voice_events(session_id)
        await manager.cancel(session_id, reason="test_cleanup")
        return session_id, events

    _session_id, events = asyncio.run(scenario())

    names = [str(event["event"]) for event in events]
    assert names == [
        "MIC_PERMISSION",
        "RECORDING_STARTED",
        "RECORDING_LEVEL",
        "RECORDING_STOPPED",
        "AUDIO_READY",
        "STT_LOADING",
        "STT_STARTED",
        "STT_COMPLETED",
        "MESSAGE_READY",
    ]
    payloads = {str(event["event"]): event["payload"] for event in events}
    assert payloads["RECORDING_STOPPED"] == {"duration_ms": 400}
    assert payloads["RECORDING_LEVEL"] == {"level": 0.35}
    assert payloads["STT_LOADING"] == {"provider": "faster_whisper", "model": "base"}
    assert "text" not in payloads["STT_COMPLETED"]


def test_voice_auto_send_requires_an_explicit_reliable_transcription(tmp_path: Path) -> None:
    async def complete_with(stt: _ImmediateResultSTT) -> tuple[dict[str, object], bool, dict[str, object]]:
        manager = VoiceSessionManager(stt=stt, tts=_NoopTTS(), storage=stt.audio_store)
        created = await manager.create(
            conversation_id=_conversation_id(tmp_path),
            device_id="test-mic",
            auto_send=True,
        )
        session_id = str(created["voice_session_id"])
        await manager.recording_started(session_id, device_id="test-mic")
        result = await manager.complete(session_id, _wav_upload())
        persisted_preference = bool(manager.get(session_id)["auto_send"])
        message_ready = next(
            event["payload"]
            for event in voice_events(session_id)
            if event["event"] == "MESSAGE_READY"
        )
        await manager.cancel(session_id, reason="test_cleanup")
        return result, persisted_preference, message_ready

    unreliable_store = TemporaryAudioStore(tmp_path / "voice-unreliable")
    unreliable, preference, event = asyncio.run(
        complete_with(
            _ImmediateResultSTT(
                unreliable_store,
                confidence=0.59,
                reliable=False,
                reliability_reason="LOW_CONFIDENCE",
            )
        )
    )
    assert unreliable["session"]["state"] == "REVIEWING"
    assert unreliable["session"]["auto_send"] is False
    assert preference is True
    assert event["auto_send"] is False

    reliable_store = TemporaryAudioStore(tmp_path / "voice-reliable")
    reliable, preference, event = asyncio.run(
        complete_with(
            _ImmediateResultSTT(
                reliable_store,
                confidence=0.91,
                reliable=True,
                reliability_reason="RELIABLE",
            )
        )
    )
    assert reliable["session"]["state"] == "REVIEWING"
    assert reliable["session"]["auto_send"] is True
    assert preference is True
    assert event["auto_send"] is True


def test_agent_runtime_exception_closes_the_bound_voice_session(tmp_path: Path, monkeypatch) -> None:
    async def failing_run_chat(*_args, **_kwargs):
        raise RuntimeError("synthetic task runtime failure")

    monkeypatch.setattr("app.runtime.task_runtime.run_chat", failing_run_chat)
    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "ask"},
        ).json()
        session_id = _reviewing_session(int(conversation["id"]))
        task_id = uuid.uuid4().hex
        submitted = client.post(
            "/api/tasks",
            json={
                "conversation_id": conversation["id"],
                "content": "ordinary voice message",
                "task_id": task_id,
                "voice_session_id": session_id,
            },
        )
        assert submitted.status_code == 202
        deadline = time.monotonic() + 5
        state = "pending"
        while time.monotonic() < deadline:
            state = str(rows("SELECT state FROM voice_sessions WHERE voice_session_id=?", (session_id,))[0]["state"])
            if state == "FAILED":
                break
            time.sleep(0.02)

    assert state == "FAILED"
    assert rows("SELECT status FROM agent_tasks WHERE id=?", (task_id,))[0]["status"] == "failed"
    names = [event["event"] for event in voice_events(session_id)]
    assert names[-2:] == ["AGENT_FAILED", "VOICE_SESSION_COMPLETED"]


def test_startup_recovery_cancels_stale_voice_session_and_deletes_temporary_audio(tmp_path: Path) -> None:
    store = TemporaryAudioStore(tmp_path / "voice-tmp")
    manager = VoiceSessionManager(stt=_NoopSTT(store), tts=_NoopTTS(), storage=store)
    conversation_id = _conversation_id(tmp_path)
    session_id = _reviewing_session(conversation_id)
    recording = store.session_dir(session_id) / "recording.wav"
    recording.write_bytes(b"private-recording-bytes")

    recovered = manager.recover_interrupted_sessions()

    assert recovered >= 1
    assert manager.get(session_id)["state"] == "CANCELLED"
    assert not recording.exists()
    events = voice_events(session_id)
    assert [(event["event"], event["payload"]) for event in events[-2:]] == [
        ("STT_CANCELLED", {"reason": "app_restarted"}),
        ("VOICE_SESSION_COMPLETED", {"status": "CANCELLED"}),
    ]


def test_microphone_permission_denial_is_persisted_without_starting_stt(tmp_path: Path) -> None:
    store = TemporaryAudioStore(tmp_path / "voice-tmp")
    tts = _NoopTTS()
    manager = VoiceSessionManager(stt=_NoopSTT(store), tts=tts, storage=store)
    conversation_id = _conversation_id(tmp_path)

    created = asyncio.run(manager.create(conversation_id=conversation_id, device_id="microphone-1"))
    denied = asyncio.run(manager.permission_denied(created["voice_session_id"]))

    assert tts.interrupt_calls == 1
    assert denied["state"] == "FAILED"
    assert denied["error_code"] == "STT_PERMISSION_DENIED"
    events = voice_events(created["voice_session_id"])
    assert [(event["event"], event["payload"]) for event in events] == [
        ("MIC_PERMISSION", {"status": "REQUESTED"}),
        ("MIC_PERMISSION", {"status": "DENIED"}),
        ("VOICE_SESSION_COMPLETED", {"status": "FAILED"}),
    ]


def test_voice_session_binds_only_to_ordinary_user_message_in_task_transaction(tmp_path: Path) -> None:
    conversation_id = _conversation_id(tmp_path)
    session_id = _reviewing_session(conversation_id)
    task_id = uuid.uuid4().hex
    transcript_for_normal_message = "请把这个转写作为普通用户消息执行"

    _create_pending_task(
        ChatRequest(
            conversation_id=conversation_id,
            content=transcript_for_normal_message,
            task_id=task_id,
            voice_session_id=session_id,
        ),
        api_key=None,
        search_credentials=None,
    )

    session = rows("SELECT task_id,message_id,state,transcription_text_hash FROM voice_sessions WHERE voice_session_id=?", (session_id,))[0]
    message = rows("SELECT id,role,content,task_id FROM messages WHERE id=?", (session["message_id"],))[0]
    assert session["task_id"] == task_id
    assert session["state"] == "QUEUED_FOR_AGENT"
    assert session["transcription_text_hash"] is None
    assert message == {"id": session["message_id"], "role": "user", "content": transcript_for_normal_message, "task_id": task_id}
    assert rows("SELECT content FROM messages WHERE conversation_id=? AND role!='user'", (conversation_id,)) == []
    assert rows("SELECT * FROM voice_sessions WHERE voice_session_id=?", (session_id,))[0].get("transcription_text_hash") is None
    _finish_test_session(session_id)


def test_voice_binding_rejects_wrong_conversation_atomically(tmp_path: Path) -> None:
    owner_conversation = _conversation_id(tmp_path)
    wrong_conversation = _conversation_id(tmp_path)
    session_id = _reviewing_session(owner_conversation)
    task_id = uuid.uuid4().hex

    with pytest.raises(VoiceSessionError) as raised:
        _create_pending_task(
            ChatRequest(
                conversation_id=wrong_conversation,
                content="cannot bypass the regular conversation boundary",
                task_id=task_id,
                voice_session_id=session_id,
            ),
            api_key=None,
            search_credentials=None,
        )

    assert raised.value.code == "VOICE_SESSION_CONFLICT"
    assert rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,)) == []
    assert rows("SELECT * FROM messages WHERE conversation_id=?", (wrong_conversation,)) == []
    assert rows("SELECT state,task_id,message_id FROM voice_sessions WHERE voice_session_id=?", (session_id,)) == [
        {"state": "REVIEWING", "task_id": None, "message_id": None}
    ]
    _finish_test_session(session_id)


def test_global_voice_stop_cancels_running_and_queued_tasks_before_voice_resources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.api.routes import voice as voice_route
    from app.runtime import runner
    from app.runtime.queue_service import claim, enqueue

    async def scenario() -> tuple[dict[str, object], dict[str, object], _TrackingTTS, _TrackingSTT, dict[str, str]]:
        conversation_id = _conversation_id(tmp_path)
        store = TemporaryAudioStore(tmp_path / "global-stop-voice")
        tts = _TrackingTTS()
        stt = _TrackingSTT(store)
        manager = VoiceSessionManager(stt=stt, tts=tts, storage=store)
        monkeypatch.setattr(voice_route, "voice_session_manager", manager)

        running_session, running_task_id = _insert_bound_voice_session(
            conversation_id=conversation_id,
            task_status="running",
            voice_state="AGENT_RUNNING",
        )
        queued_session, queued_task_id = _insert_bound_voice_session(
            conversation_id=conversation_id,
            task_status="pending",
            voice_state="QUEUED_FOR_AGENT",
        )
        tts_session, completed_task_id = _insert_bound_voice_session(
            conversation_id=conversation_id,
            task_status="completed",
            voice_state="TTS_PLAYING",
        )
        stt_session = uuid.uuid4().hex
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT INTO voice_sessions(voice_session_id,conversation_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
                (stt_session, conversation_id, "TRANSCRIBING", stamp, stamp),
            )

        running_queue = enqueue(
            conversation_id=conversation_id,
            task_id=running_task_id,
            kind="submit",
            content="running voice task",
            payload={"task_id": running_task_id},
        )
        assert claim(running_queue.id) is not None
        queued_queue = enqueue(
            conversation_id=conversation_id,
            task_id=queued_task_id,
            kind="submit",
            content="queued voice task",
            payload={"task_id": queued_task_id},
        )

        started = asyncio.Event()

        async def owned_running_task() -> None:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                with connect() as db:
                    db.execute(
                        "UPDATE agent_tasks SET status='cancelled',updated_at=?,finished_at=? WHERE id=?",
                        (now_iso(), now_iso(), running_task_id),
                    )
                raise

        owned = asyncio.create_task(owned_running_task())
        await started.wait()
        runner._running_tasks[running_task_id] = owned
        try:
            result = await voice_route.stop(voice_route.StopInput())
            repeated = await voice_route.stop(voice_route.StopInput())
        finally:
            runner._running_tasks.pop(running_task_id, None)
            await asyncio.gather(owned, return_exceptions=True)

        return result, repeated, tts, stt, {
            "running_session": running_session,
            "queued_session": queued_session,
            "tts_session": tts_session,
            "stt_session": stt_session,
            "running_task": running_task_id,
            "queued_task": queued_task_id,
            "completed_task": completed_task_id,
            "running_queue": running_queue.id,
            "queued_queue": queued_queue.id,
        }

    result, repeated, tts, stt, ids = asyncio.run(scenario())

    assert result["status"] == "CANCELLED"
    assert result["scope"] == "all"
    assert result["sessions_targeted"] == 4
    assert result["sessions_cancelled"] == 4
    assert repeated["status"] == "CANCELLED"
    assert repeated["sessions_targeted"] == 0
    assert repeated["summary"]["target_count"] == 0
    assert {item["task_id"] for item in result["agent_tasks"]} == {
        ids["running_task"],
        ids["queued_task"],
        ids["completed_task"],
    }
    task_results = {item["task_id"]: item for item in result["agent_tasks"]}
    assert task_results[ids["running_task"]]["status"] == "cancelled"
    assert task_results[ids["queued_task"]]["status"] == "cancelled"
    assert task_results[ids["completed_task"]]["status"] == "completed"
    assert all(not item["active"] and item["queue_items_active"] == 0 for item in result["agent_tasks"])

    assert rows(
        "SELECT id,status FROM agent_tasks WHERE id IN (?,?,?) ORDER BY id",
        (ids["running_task"], ids["queued_task"], ids["completed_task"]),
    ) == sorted(
        [
            {"id": ids["running_task"], "status": "cancelled"},
            {"id": ids["queued_task"], "status": "cancelled"},
            {"id": ids["completed_task"], "status": "completed"},
        ],
        key=lambda item: item["id"],
    )
    assert rows(
        "SELECT id,status FROM conversation_queue_items WHERE id IN (?,?) ORDER BY id",
        (ids["running_queue"], ids["queued_queue"]),
    ) == sorted(
        [
            {"id": ids["running_queue"], "status": "cancelled"},
            {"id": ids["queued_queue"], "status": "cancelled"},
        ],
        key=lambda item: item["id"],
    )
    assert rows(
        "SELECT voice_session_id,state FROM voice_sessions WHERE voice_session_id IN (?,?,?,?) ORDER BY voice_session_id",
        (ids["running_session"], ids["queued_session"], ids["tts_session"], ids["stt_session"]),
    ) == sorted(
        [
            {"voice_session_id": ids["running_session"], "state": "CANCELLED"},
            {"voice_session_id": ids["queued_session"], "state": "CANCELLED"},
            {"voice_session_id": ids["tts_session"], "state": "CANCELLED"},
            {"voice_session_id": ids["stt_session"], "state": "CANCELLED"},
        ],
        key=lambda item: item["voice_session_id"],
    )
    assert tts.task_ids[-1] is None
    # STT owns only capture/transcription phases; Agent and TTS phases must
    # not signal an unrelated STT worker during a global stop.
    assert stt.voice_session_ids == [ids["stt_session"]]
    assert "AGENT_CANCELLED" not in [event["event"] for event in voice_events(ids["tts_session"])]
    audit_rows = rows(
        "SELECT action,target,status,details FROM audit_logs WHERE action='voice_global_stop' ORDER BY id DESC LIMIT 2"
    )
    assert len(audit_rows) == 2
    audit_details = [json.loads(item["details"]) for item in reversed(audit_rows)]
    assert all(item["target"] == "all" and item["status"] == "CANCELLED" for item in audit_rows)
    assert audit_details[0] == {
        "schema_version": 1,
        "scope": "all",
        "requested_session": False,
        "sessions_targeted": 4,
        "sessions_cancelled": 4,
        "target_count": 4,
        "active_count": 0,
        "queue_active_count": 0,
        "unresolved_count": 0,
        "settled": True,
        "idempotent_no_active_target": False,
    }
    assert audit_details[1] == {
        "schema_version": 1,
        "scope": "all",
        "requested_session": False,
        "sessions_targeted": 0,
        "sessions_cancelled": 0,
        "target_count": 0,
        "active_count": 0,
        "queue_active_count": 0,
        "unresolved_count": 0,
        "settled": True,
        "idempotent_no_active_target": True,
    }
    serialized_audit = json.dumps(audit_details, ensure_ascii=False)
    assert all(value not in serialized_audit for value in ids.values())
    assert "voice stop test" not in serialized_audit


def test_specific_voice_stop_is_scoped_and_idempotent(tmp_path: Path) -> None:
    from app.runtime.queue_service import enqueue

    async def scenario() -> tuple[dict[str, object], dict[str, object], _TrackingTTS, _TrackingSTT, dict[str, str]]:
        conversation_id = _conversation_id(tmp_path)
        store = TemporaryAudioStore(tmp_path / "specific-stop-voice")
        tts = _TrackingTTS()
        stt = _TrackingSTT(store)
        manager = VoiceSessionManager(stt=stt, tts=tts, storage=store)
        target_session, target_task = _insert_bound_voice_session(
            conversation_id=conversation_id,
            task_status="pending",
            voice_state="QUEUED_FOR_AGENT",
        )
        other_session, other_task = _insert_bound_voice_session(
            conversation_id=conversation_id,
            task_status="pending",
            voice_state="QUEUED_FOR_AGENT",
        )
        target_queue = enqueue(
            conversation_id=conversation_id,
            task_id=target_task,
            kind="submit",
            content="target",
            payload={"task_id": target_task},
        )
        other_queue = enqueue(
            conversation_id=conversation_id,
            task_id=other_task,
            kind="submit",
            content="other",
            payload={"task_id": other_task},
        )

        first = await manager.stop(target_session)
        event_count = len(voice_events(target_session))
        tts_calls = len(tts.task_ids)
        stt_calls = len(stt.voice_session_ids)
        second = await manager.stop(target_session)
        assert len(voice_events(target_session)) == event_count
        assert len(tts.task_ids) == tts_calls
        assert len(stt.voice_session_ids) == stt_calls

        return first, second, tts, stt, {
            "target_session": target_session,
            "target_task": target_task,
            "target_queue": target_queue.id,
            "other_session": other_session,
            "other_task": other_task,
            "other_queue": other_queue.id,
        }

    first, second, tts, stt, ids = asyncio.run(scenario())

    assert first["status"] == "CANCELLED"
    assert first["scope"] == "session"
    assert first["sessions_targeted"] == 1
    assert first["sessions_cancelled"] == 1
    assert second["status"] == "CANCELLED"
    assert second["sessions_targeted"] == 0
    assert second["sessions_cancelled"] == 0
    assert second["sessions"][0]["actions"]["status"] == "ALREADY_TERMINAL"
    assert tts.task_ids == [ids["target_task"]]
    assert stt.voice_session_ids == []
    assert rows("SELECT status FROM agent_tasks WHERE id=?", (ids["target_task"],))[0]["status"] == "cancelled"
    assert rows("SELECT status FROM conversation_queue_items WHERE id=?", (ids["target_queue"],))[0]["status"] == "cancelled"
    assert rows("SELECT state FROM voice_sessions WHERE voice_session_id=?", (ids["target_session"],))[0]["state"] == "CANCELLED"
    assert rows("SELECT status FROM agent_tasks WHERE id=?", (ids["other_task"],))[0]["status"] == "pending"
    assert rows("SELECT status FROM conversation_queue_items WHERE id=?", (ids["other_queue"],))[0]["status"] == "pending"
    assert rows("SELECT state FROM voice_sessions WHERE voice_session_id=?", (ids["other_session"],))[0]["state"] == "QUEUED_FOR_AGENT"

    # Leave no active test-owned rows behind for later module tests.
    cleanup_manager = VoiceSessionManager(
        stt=_TrackingSTT(TemporaryAudioStore(tmp_path / "specific-stop-cleanup")),
        tts=_TrackingTTS(),
    )
    asyncio.run(cleanup_manager.stop(ids["other_session"], reason="test_cleanup"))


def test_voice_stop_does_not_claim_agent_cancelled_before_runtime_settles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.api.routes import voice as voice_route
    from app.runtime import runner

    async def scenario() -> tuple[dict[str, object], dict[str, object], str, str]:
        conversation_id = _conversation_id(tmp_path)
        store = TemporaryAudioStore(tmp_path / "unsettled-stop-voice")
        manager = VoiceSessionManager(
            stt=_TrackingSTT(store),
            tts=_TrackingTTS(),
            storage=store,
        )
        manager._TASK_CANCELLATION_SETTLE_SECONDS = 0.0
        monkeypatch.setattr(voice_route, "voice_session_manager", manager)
        session_id, task_id = _insert_bound_voice_session(
            conversation_id=conversation_id,
            task_status="running",
            voice_state="AGENT_RUNNING",
        )
        private_prompt = "PRIVATE TASK BODY MUST NOT LEAVE THE STOP RESPONSE"
        with connect() as db:
            db.execute("UPDATE agent_tasks SET prompt=? WHERE id=?", (private_prompt, task_id))

        def request_without_settlement(requested_task_id: str) -> dict[str, object]:
            assert requested_task_id == task_id
            with connect() as db:
                db.execute(
                    "UPDATE agent_tasks SET status='cancel_requested',updated_at=? WHERE id=?",
                    (now_iso(), task_id),
                )
            return {"id": task_id, "status": "cancel_requested", "interrupted": True}

        monkeypatch.setattr(runner, "cancel_task", request_without_settlement)
        pending = await voice_route.stop(voice_route.StopInput(voice_session_id=session_id))
        assert private_prompt not in json.dumps(pending, ensure_ascii=False)
        assert manager.get(session_id)["state"] == "CANCEL_REQUESTED"
        assert "AGENT_CANCELLED" not in [event["event"] for event in voice_events(session_id)]
        assert "VOICE_SESSION_COMPLETED" not in [event["event"] for event in voice_events(session_id)]

        with connect() as db:
            db.execute(
                "UPDATE agent_tasks SET status='cancelled',updated_at=?,finished_at=? WHERE id=?",
                (now_iso(), now_iso(), task_id),
            )
        assert await manager.agent_finished(task_id, task_status="cancelled") == 1
        return pending, manager.get(session_id), session_id, task_id

    pending, settled, session_id, task_id = asyncio.run(scenario())

    assert pending["status"] == "CANCEL_REQUESTED"
    assert pending["sessions_cancelled"] == 0
    assert pending["agent_tasks"] == [
        {
            "task_id": task_id,
            "status_before": "running",
            "status": "cancel_requested",
            "cancelled": False,
            "active": True,
            "settled": False,
            "interrupted": True,
            "runtime_status": "cancel_requested",
            "runtime_error_code": None,
            "queue_items": [],
            "queue_items_active": 0,
        }
    ]
    assert settled["state"] == "CANCELLED"
    assert [event["event"] for event in voice_events(session_id)][-2:] == [
        "AGENT_CANCELLED",
        "VOICE_SESSION_COMPLETED",
    ]


@pytest.mark.parametrize("resource", ["tts", "stt"])
def test_voice_stop_stays_cancel_requested_until_resource_owner_settles(
    tmp_path: Path,
    resource: str,
) -> None:
    async def scenario() -> tuple[dict[str, object], str]:
        conversation_id = _conversation_id(tmp_path)
        store = TemporaryAudioStore(tmp_path / f"resource-stop-{resource}")
        tts = _UnsettledTTS() if resource == "tts" else _TrackingTTS()
        stt = _UnsettledSTT(store) if resource == "stt" else _TrackingSTT(store)
        manager = VoiceSessionManager(stt=stt, tts=tts, storage=store)
        session_id = uuid.uuid4().hex
        stamp = now_iso()
        task_id = uuid.uuid4().hex if resource == "tts" else None
        with connect() as db:
            if task_id:
                db.execute(
                    "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (task_id, conversation_id, "completed", "resource stop", stamp, stamp),
                )
            db.execute(
                "INSERT INTO voice_sessions(voice_session_id,conversation_id,task_id,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    session_id,
                    conversation_id,
                    task_id,
                    "TTS_PLAYING" if task_id else "TRANSCRIBING",
                    stamp,
                    stamp,
                ),
            )
        result = await manager.stop(session_id)
        return result, manager.get(session_id)["state"]

    result, state = asyncio.run(scenario())
    assert result["status"] == "CANCEL_REQUESTED"
    assert result["sessions_cancelled"] == 0
    assert state == "CANCEL_REQUESTED"
    assert "VOICE_SESSION_COMPLETED" not in [
        event["event"] for event in voice_events(result["requested_voice_session_id"])
    ]


@pytest.mark.parametrize("resource", ("tts", "orphan_tts", "stt"))
def test_voice_cancel_does_not_terminalize_when_resource_interrupt_raises(
    tmp_path: Path,
    resource: str,
) -> None:
    """A caught owner exception remains an unresolved cancellation, not PASS."""

    async def scenario() -> tuple[dict[str, object], dict[str, object], str, str]:
        conversation_id = _conversation_id(tmp_path)
        store = TemporaryAudioStore(tmp_path / f"resource-raise-{resource}")
        tts = _RaisingTTS() if resource in {"tts", "orphan_tts"} else _TrackingTTS()
        stt = _RaisingSTT(store) if resource == "stt" else _TrackingSTT(store)
        manager = VoiceSessionManager(stt=stt, tts=tts, storage=store)
        session_id = uuid.uuid4().hex
        task_id = uuid.uuid4().hex if resource == "tts" else None
        stamp = now_iso()
        with connect() as db:
            if task_id:
                db.execute(
                    "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (task_id, conversation_id, "completed", "resource raise", stamp, stamp),
                )
            db.execute(
                "INSERT INTO voice_sessions(voice_session_id,conversation_id,task_id,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    session_id,
                    conversation_id,
                    task_id,
                    "TTS_PLAYING" if resource in {"tts", "orphan_tts"} else "TRANSCRIBING",
                    stamp,
                    stamp,
                ),
            )
        first = await manager.cancel(session_id)
        repeated = await manager.cancel(session_id)
        return first, repeated, manager.get(session_id)["state"], session_id

    first, repeated, state, session_id = asyncio.run(scenario())
    assert first["state"] == "CANCEL_REQUESTED"
    assert repeated["state"] == "CANCEL_REQUESTED"
    assert state == "CANCEL_REQUESTED"
    assert "VOICE_SESSION_COMPLETED" not in [event["event"] for event in voice_events(session_id)]


def test_agent_finished_rechecks_unsettled_tts_before_cancelling_voice_session(tmp_path: Path) -> None:
    """A later Agent event cannot bypass an earlier unsettled TTS receipt."""

    async def scenario() -> tuple[dict[str, object], str, str]:
        conversation_id = _conversation_id(tmp_path)
        store = TemporaryAudioStore(tmp_path / "agent-finished-unsettled-tts")
        manager = VoiceSessionManager(stt=_TrackingSTT(store), tts=_UnsettledTTS(), storage=store)
        session_id, task_id = _insert_bound_voice_session(
            conversation_id=conversation_id,
            task_status="completed",
            voice_state="TTS_PLAYING",
        )
        pending = await manager.stop(session_id)
        with connect() as db:
            db.execute(
                "UPDATE agent_tasks SET status='cancelled',updated_at=?,finished_at=? WHERE id=?",
                (now_iso(), now_iso(), task_id),
            )
        assert await manager.agent_finished(task_id, task_status="cancelled") == 1
        return pending, manager.get(session_id)["state"], session_id

    pending, state, session_id = asyncio.run(scenario())
    assert pending["status"] == "CANCEL_REQUESTED"
    assert state == "CANCEL_REQUESTED"
    events = [event["event"] for event in voice_events(session_id)]
    assert "AGENT_CANCELLED" not in events
    assert "VOICE_SESSION_COMPLETED" not in events


def test_global_voice_stop_keeps_sessions_pending_when_unbound_tts_interrupt_raises(tmp_path: Path) -> None:
    """The global receipt is a prerequisite for durable Voice cancellation."""

    async def scenario() -> tuple[dict[str, object], str, str]:
        conversation_id = _conversation_id(tmp_path)
        store = TemporaryAudioStore(tmp_path / "global-tts-raise")
        manager = VoiceSessionManager(stt=_TrackingSTT(store), tts=_GlobalRaisingTTS(), storage=store)
        session_id, _task_id = _insert_bound_voice_session(
            conversation_id=conversation_id,
            task_status="completed",
            voice_state="TTS_PLAYING",
        )
        result = await manager.stop()
        return result, manager.get(session_id)["state"], session_id

    result, state, session_id = asyncio.run(scenario())
    assert result["status"] == "CANCEL_REQUESTED"
    assert result["tts"] == {"status": "FAILED", "error_code": "RuntimeError"}
    assert result["sessions_cancelled"] == 0
    assert state == "CANCEL_REQUESTED"
    assert "VOICE_SESSION_COMPLETED" not in [event["event"] for event in voice_events(session_id)]
