from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from app.tts.providers import windows_tts
from app.tts.schemas import SynthesisRequest


def capture_production_script(tmp_path: Path, monkeypatch, text: str) -> tuple[str, bytes]:
    output = tmp_path / "synthetic.wav"
    captured = {}

    class Process:
        pid = 987654
        returncode = 0

        async def communicate(self, data):
            captured["input"] = data
            with wave.open(str(output), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\0\0" * 1600)
            return b"", b""

    async def create(*arguments, **options):
        captured["script"] = arguments[-1]
        assert options["stdin"] == asyncio.subprocess.PIPE
        assert text not in captured["script"]
        return Process()

    monkeypatch.setattr(windows_tts.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(windows_tts, "register_process", lambda *args: None)
    monkeypatch.setattr(windows_tts, "unregister_process", lambda *args: None)
    provider = windows_tts.WindowsTTSProvider()
    request = SynthesisRequest("encoding", None, None, text, "", 1.0, 1.0, 24000, "NORMAL", False, "encoding")
    asyncio.run(provider.synthesize(request, output))
    return captured["script"], captured["input"]


def test_production_tts_decodes_utf8_before_console_read(tmp_path, monkeypatch):
    text = "这是司忆中文语音测试。"
    script, data = capture_production_script(tmp_path, monkeypatch, text)
    assert data == text.encode("utf-8")
    assert script.index("[Console]::InputEncoding=[Text.UTF8Encoding]::new($false,$true)") < script.index("[Console]::In.ReadToEnd()")
    assert script.index("[Console]::OutputEncoding") < script.index("[Console]::In.ReadToEnd()")


@pytest.mark.skipif(sys.platform != "win32", reason="actual Windows PowerShell stdin boundary")
@pytest.mark.parametrize("text", ["这是本地语音转写测试。", "司忆：中文、英文 English 和表情 🙂", "引号 ' \" $([1+1]) ` 换行\n仍是数据"])
def test_actual_powershell_roundtrip_uses_captured_production_encoding(tmp_path, monkeypatch, text):
    script, data = capture_production_script(tmp_path, monkeypatch, text)
    prefix = script.partition("Add-Type")[0]
    # Execute only the production encoding prelude, never SAPI synthesis or
    # user text as code. This is a silent Windows-only integration check.
    reader = prefix + "$text=[Console]::In.ReadToEnd();@{text=$text;codepage=[Console]::InputEncoding.CodePage}|ConvertTo-Json -Compress"
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", reader],
                            input=data, capture_output=True, check=True, timeout=15, creationflags=0x08000000)
    observed = json.loads(result.stdout.decode("utf-8-sig"))
    assert observed == {"text": text, "codepage": 65001}
