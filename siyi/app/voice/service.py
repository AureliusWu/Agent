from __future__ import annotations

from .contracts import AudioInput, SpeechOutput, SpeechRequest, Transcript
from .providers.mock import MockSTTProvider, MockTTSProvider
from .registry import VoiceProviderRegistry


class VoiceService:
    def __init__(
        self,
        registry: VoiceProviderRegistry,
        *,
        default_stt: str,
        default_tts: str,
    ) -> None:
        self.registry = registry
        self.default_stt = default_stt
        self.default_tts = default_tts

    async def transcribe(self, audio: AudioInput, *, provider_id: str | None = None) -> Transcript:
        provider = self.registry.get_stt(provider_id or self.default_stt)
        return await provider.transcribe(audio)

    async def synthesize(self, request: SpeechRequest, *, provider_id: str | None = None) -> SpeechOutput:
        provider = self.registry.get_tts(provider_id or self.default_tts)
        return await provider.synthesize(request)

    async def health(self) -> dict[str, object]:
        stt = {}
        for provider_id in self.registry.stt_ids:
            try:
                stt[provider_id] = await self.registry.get_stt(provider_id).health()
            except Exception as exc:
                stt[provider_id] = {"status": "error", "error": str(exc)}
        tts = {}
        for provider_id in self.registry.tts_ids:
            try:
                tts[provider_id] = await self.registry.get_tts(provider_id).health()
            except Exception as exc:
                tts[provider_id] = {"status": "error", "error": str(exc)}
        return {
            "status": "ok",
            "default_stt": self.default_stt,
            "default_tts": self.default_tts,
            "stt": stt,
            "tts": tts,
        }


def create_default_voice_service() -> VoiceService:
    registry = VoiceProviderRegistry()
    registry.register_stt(MockSTTProvider())
    registry.register_tts(MockTTSProvider())
    return VoiceService(registry, default_stt="mock-stt", default_tts="mock-tts")
