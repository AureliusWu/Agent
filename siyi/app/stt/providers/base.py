from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from app.stt.schemas import TranscriptionRequest, TranscriptionResult


class STTProvider(ABC):
    id: str
    name: str
    maturity: str = "experimental"

    @abstractmethod
    def health_check(self) -> dict[str, Any]: ...

    @abstractmethod
    def load_model(self, model_id: str, model_path: Path, *, device: str, compute_type: str) -> dict[str, Any]: ...

    @abstractmethod
    def unload_model(self) -> dict[str, Any]: ...

    @abstractmethod
    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...

    @abstractmethod
    def status(self) -> dict[str, Any]: ...
