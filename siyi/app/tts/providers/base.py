from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.tts.schemas import SynthesisRequest


class TTSProviderError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class TTSProvider(ABC):
    id: str
    name: str
    version: str
    device: str

    @abstractmethod
    async def health_check(self) -> dict: ...

    @abstractmethod
    async def list_voices(self) -> list[dict]: ...

    @abstractmethod
    async def synthesize(self, request: SynthesisRequest, output: Path) -> dict: ...

    @abstractmethod
    async def cancel(self, request_id: str) -> bool: ...

    @abstractmethod
    async def unload(self) -> dict: ...

    @abstractmethod
    def get_status(self) -> dict: ...

    @abstractmethod
    def get_metrics(self) -> dict: ...
