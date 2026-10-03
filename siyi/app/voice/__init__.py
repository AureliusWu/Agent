"""Provider-oriented voice I/O primitives for Siyi."""

from .contracts import AudioInput, SpeechOutput, SpeechRequest, Transcript, STTProvider, TTSProvider
from .registry import VoiceProviderRegistry
from .service import VoiceService, create_default_voice_service

__all__ = [
    "AudioInput",
    "SpeechOutput",
    "SpeechRequest",
    "Transcript",
    "STTProvider",
    "TTSProvider",
    "VoiceProviderRegistry",
    "VoiceService",
    "create_default_voice_service",
]
