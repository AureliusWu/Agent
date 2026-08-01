from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


TTSStatus = Literal[
    "QUEUED", "SYNTHESIZING", "READY", "PLAYING", "COMPLETED",
    "CANCEL_REQUESTED", "CANCELLED", "FAILED",
]


@dataclass(frozen=True)
class SynthesisRequest:
    request_id: str
    task_id: str | None
    message_id: str | None
    text: str
    voice: str
    speed: float
    volume: float
    sample_rate: int
    priority: str
    cache: bool
    idempotency_key: str


@dataclass(frozen=True)
class SynthesisResult:
    request_id: str
    provider: str
    status: TTSStatus
    audio_url: str | None
    duration_ms: int
    sample_rate: int
    cached: bool
    synthesis_ms: float
    error: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)
