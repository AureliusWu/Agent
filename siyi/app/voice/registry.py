from __future__ import annotations

from .contracts import STTProvider, TTSProvider


class VoiceProviderRegistry:
    """Small, explicit registry so voice vendors stay outside Agent Core."""

    def __init__(self) -> None:
        self._stt: dict[str, STTProvider] = {}
        self._tts: dict[str, TTSProvider] = {}

    def register_stt(self, provider: STTProvider, *, replace: bool = False) -> None:
        self._register(self._stt, provider.provider_id, provider, replace=replace)

    def register_tts(self, provider: TTSProvider, *, replace: bool = False) -> None:
        self._register(self._tts, provider.provider_id, provider, replace=replace)

    @staticmethod
    def _register(store: dict, provider_id: str, provider: object, *, replace: bool) -> None:
        normalized = provider_id.strip()
        if not normalized:
            raise ValueError("voice provider_id cannot be empty")
        if normalized in store and not replace:
            raise ValueError(f"voice provider already registered: {normalized}")
        store[normalized] = provider

    def get_stt(self, provider_id: str) -> STTProvider:
        try:
            return self._stt[provider_id]
        except KeyError as exc:
            raise KeyError(f"unknown STT provider: {provider_id}") from exc

    def get_tts(self, provider_id: str) -> TTSProvider:
        try:
            return self._tts[provider_id]
        except KeyError as exc:
            raise KeyError(f"unknown TTS provider: {provider_id}") from exc

    @property
    def stt_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._stt))

    @property
    def tts_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tts))
