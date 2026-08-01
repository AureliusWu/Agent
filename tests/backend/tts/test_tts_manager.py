from __future__ import annotations

import asyncio
import wave
from pathlib import Path
import pytest

from app.tts.cache import AudioCache
from app.tts.manager import TTSManager, create_request
from app.tts.providers.base import TTSProvider, TTSProviderError


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
    async def health_check(self): return {"provider": self.id, "status": "ok"}
    async def list_voices(self): return [{"name": "zh", "culture": "zh-CN"}]
    async def synthesize(self, request, output):
        self.calls += 1
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000); handle.writeframes(b"\x00\x00" * 2400)
        return {"duration_ms": 100, "sample_rate": 24000, "synthesis_ms": 5.0}
    async def cancel(self, request_id): return True
    async def unload(self): return {"status": "unloaded"}
    def get_status(self): return {"provider": self.id, "active_requests": []}
    def get_metrics(self): return {"calls": self.calls}


def manager(tmp_path: Path) -> TTSManager:
    target = TTSManager(AudioCache(tmp_path / "cache"))
    target.temp = tmp_path / "temp"; target.temp.mkdir()
    target.providers = {"melotts": FakeUnavailable(), "windows": FakeWindows()}
    target.update_settings({"provider": "melotts", "fallback_provider": "windows", "allow_fallback": True, "cache_enabled": True})
    return target


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


def test_sensitive_text_is_never_cached(tmp_path: Path) -> None:
    target = manager(tmp_path)
    result = asyncio.run(target.synthesize(create_request({"request_id":"sensitive","idempotency_key":"sensitive","text":"正常提示。password: hidden-value","cache":True})))
    assert result["cached"] is False
    assert target.cache_list() == []


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


def test_cache_key_changes_with_voice_and_speed() -> None:
    base = {"provider":"windows","model_version":"1","voice":"a","normalized_text":"text","speed":1.0,"sample_rate":24000}
    first = AudioCache.key(**base)
    assert first != AudioCache.key(**{**base, "voice":"b"})
    assert first != AudioCache.key(**{**base, "speed":1.2})
