import asyncio

import pytest

from app.voice import AudioInput, SpeechRequest, VoiceProviderRegistry, VoiceService
from app.voice.providers.mock import MockSTTProvider, MockTTSProvider


def _service() -> VoiceService:
    registry = VoiceProviderRegistry()
    registry.register_stt(MockSTTProvider())
    registry.register_tts(MockTTSProvider())
    return VoiceService(registry, default_stt="mock-stt", default_tts="mock-tts")


def test_registry_rejects_duplicate_provider() -> None:
    registry = VoiceProviderRegistry()
    registry.register_stt(MockSTTProvider())
    with pytest.raises(ValueError, match="already registered"):
        registry.register_stt(MockSTTProvider())


def test_mock_stt_round_trip_for_text_fixture() -> None:
    result = asyncio.run(
        _service().transcribe(
            AudioInput(data="你好，司忆".encode("utf-8"), mime_type="text/plain", language="zh-CN")
        )
    )
    assert result.text == "你好，司忆"
    assert result.provider_id == "mock-stt"
    assert result.language == "zh-CN"


def test_mock_tts_returns_playable_wav_container() -> None:
    result = asyncio.run(_service().synthesize(SpeechRequest(text="语音插件雏形")))
    assert result.provider_id == "mock-tts"
    assert result.mime_type == "audio/wav"
    assert result.data[:4] == b"RIFF"
    assert result.data[8:12] == b"WAVE"
    assert result.duration_ms is not None and result.duration_ms > 0


def test_voice_health_lists_registered_providers() -> None:
    health = asyncio.run(_service().health())
    assert health["status"] == "ok"
    assert health["default_stt"] == "mock-stt"
    assert health["default_tts"] == "mock-tts"
    assert "mock-stt" in health["stt"]
    assert "mock-tts" in health["tts"]
