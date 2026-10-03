from __future__ import annotations

import io
import wave

from ..contracts import AudioInput, SpeechOutput, SpeechRequest, Transcript


class MockSTTProvider:
    provider_id = "mock-stt"

    async def health(self) -> dict[str, object]:
        return {"status": "ok", "provider_id": self.provider_id, "mode": "mock"}

    async def transcribe(self, audio: AudioInput) -> Transcript:
        if not audio.data:
            raise ValueError("audio payload is empty")
        if audio.mime_type.startswith("text/"):
            text = audio.data.decode("utf-8").strip()
        else:
            text = f"[mock transcript: {len(audio.data)} bytes]"
        return Transcript(
            text=text,
            provider_id=self.provider_id,
            language=audio.language,
        )


class MockTTSProvider:
    provider_id = "mock-tts"
    sample_rate_hz = 16_000

    async def health(self) -> dict[str, object]:
        return {"status": "ok", "provider_id": self.provider_id, "mode": "mock"}

    async def synthesize(self, request: SpeechRequest) -> SpeechOutput:
        text = request.text.strip()
        if not text:
            raise ValueError("speech text is empty")
        duration_ms = min(1500, max(250, len(text) * 20))
        frame_count = int(self.sample_rate_hz * duration_ms / 1000)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate_hz)
            wav.writeframes(b"\x00\x00" * frame_count)
        return SpeechOutput(
            data=buffer.getvalue(),
            mime_type="audio/wav",
            provider_id=self.provider_id,
            duration_ms=duration_ms,
        )
