from __future__ import annotations

import asyncio
import os
import wave
from pathlib import Path

import pytest

from app.tts.providers.windows_tts import WindowsTTSProvider
from app.tts.schemas import SynthesisRequest


pytestmark = pytest.mark.skipif(os.getenv("SIYI_TEST_TTS") != "windows", reason="requires explicit Windows TTS selection")


def test_windows_tts_generates_real_chinese_wave(tmp_path: Path) -> None:
    provider = WindowsTTSProvider()
    voices = asyncio.run(provider.list_voices())
    voice = next(item["name"] for item in voices if item.get("culture") == "zh-CN")
    output = tmp_path / "speech.wav"
    result = asyncio.run(provider.synthesize(SynthesisRequest("live", None, None, "你好，这是司忆的本地中文语音测试。", voice, 1.0, 1.0, 24000, "NORMAL", False, "live"), output))
    with wave.open(str(output), "rb") as handle:
        assert handle.getnframes() > 1000
    assert output.stat().st_size > 10_000
    assert result["duration_ms"] > 500
    assert provider.device == "cpu"


def test_windows_tts_can_be_interrupted_without_residual_process(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = WindowsTTSProvider()
        output = tmp_path / "cancelled.wav"
        request = SynthesisRequest("cancel-live", "task", None, "这是一段用于验证立即停止能力的本地语音。" * 10, "", 0.5, 1.0, 24000, "NORMAL", False, "cancel-live")
        running = asyncio.create_task(provider.synthesize(request, output))
        for _ in range(50):
            if provider.get_status()["active_requests"]:
                break
            await asyncio.sleep(0.02)
        assert await provider.cancel("cancel-live") is True
        with pytest.raises(Exception):
            await running
        assert provider.get_status()["active_requests"] == []
        assert not output.exists() or output.stat().st_size <= 46

    asyncio.run(scenario())
