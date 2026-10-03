from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AudioInput:
    data: bytes
    mime_type: str = "application/octet-stream"
    language: str | None = None


@dataclass(frozen=True)
class Transcript:
    text: str
    provider_id: str
    language: str | None = None
    duration_ms: int | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice: str | None = None
    speed: float = 1.0
    audio_format: str = "wav"


@dataclass(frozen=True)
class SpeechOutput:
    data: bytes
    mime_type: str
    provider_id: str
    duration_ms: int | None = None


class STTProvider(Protocol):
    provider_id: str

    async def health(self) -> dict[str, object]:
        ...

    async def transcribe(self, audio: AudioInput) -> Transcript:
        ...


class TTSProvider(Protocol):
    provider_id: str

    async def health(self) -> dict[str, object]:
        ...

    async def synthesize(self, request: SpeechRequest) -> SpeechOutput:
        ...
