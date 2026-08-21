from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal


DEFAULT_STT_MODEL_ID = "small"
MIN_AUTO_SEND_CONFIDENCE = 0.60

STTStatus = Literal[
    "NOT_CONFIGURED",
    "MODEL_MISSING",
    "DOWNLOADING",
    "LOADING",
    "READY",
    "TRANSCRIBING",
    "UNLOADING",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "FAILED",
]
STT_STATUS_VALUES = frozenset(
    {
        "NOT_CONFIGURED",
        "MODEL_MISSING",
        "DOWNLOADING",
        "LOADING",
        "READY",
        "TRANSCRIBING",
        "UNLOADING",
        "CANCEL_REQUESTED",
        "CANCELLED",
        "FAILED",
    }
)


class STTError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class STTModel:
    id: str
    provider: str
    repo_id: str
    estimated_bytes: int
    description: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptionRequest:
    request_id: str
    voice_session_id: str
    audio_path: str
    audio_sha256: str
    audio_duration_ms: int
    language: str = "zh"
    model_id: str = DEFAULT_STT_MODEL_ID
    device: str = "cpu"
    compute_type: str = "int8"
    vad: bool = True
    timestamps: bool = False


@dataclass(frozen=True)
class TranscriptionResult:
    request_id: str
    provider: str
    model: str
    language: str
    text: str
    segments: list[dict[str, Any]]
    duration_ms: int
    transcription_ms: float
    real_time_factor: float | None
    confidence: float | None = None
    reliable: bool = False
    reliability_reason: str = "CONFIDENCE_UNAVAILABLE"
    cancelled: bool = False
    error: dict[str, str] | None = None

    @property
    def auto_send_safe(self) -> bool:
        """Require an explicit provider confidence decision for auto-send.

        Missing/legacy confidence metadata deliberately fails closed.  The
        transcript remains available for review and manual editing.
        """

        return bool(
            self.text.strip()
            and self.reliable
            and self.confidence is not None
            and math.isfinite(self.confidence)
            and self.confidence >= MIN_AUTO_SEND_CONFIDENCE
            and self.reliability_reason == "RELIABLE"
            and not self.cancelled
            and self.error is None
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
