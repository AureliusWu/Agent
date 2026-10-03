"""Replaceable speech protocol and a bounded OpenAI-compatible HTTP adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.security.network_security import guarded_request

AudioFormat = Literal["mp3", "wav"]


class SpeechProviderError(ValueError):
    pass


@dataclass(frozen=True)
class TranscriptionRequest:
    audio: bytes = field(repr=False)
    media_type: str
    language: str = ""


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    model: str


@dataclass(frozen=True)
class SynthesisRequest:
    text: str = field(repr=False)
    format: AudioFormat = "mp3"


@dataclass(frozen=True)
class SynthesisResult:
    audio: bytes = field(repr=False)
    media_type: str
    model: str
    voice: str


class SpeechAdapter(Protocol):
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...
    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult: ...


@dataclass(frozen=True)
class OpenAICompatibleSpeechAdapter:
    base_url: str
    api_key: str = field(repr=False)
    transcription_model: str = ""
    synthesis_model: str = ""
    voice: str = ""
    timeout_seconds: int = 60
    max_audio_bytes: int = 20_000_000

    def endpoint(self, path: str) -> str:
        base = self.base_url.strip().rstrip("/")
        parsed = urlsplit(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SpeechProviderError("语音服务地址必须为不含凭据、查询参数和片段的 HTTPS 基础地址")
        return f"{base}/{path}"

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        if not self.api_key or not self.transcription_model:
            raise SpeechProviderError("语音识别服务未配置")
        if not request.audio or len(request.audio) > self.max_audio_bytes:
            raise SpeechProviderError("音频为空或超过语音服务的输入限制")
        data = {"model": self.transcription_model, "response_format": "json"}
        if request.language:
            data["language"] = request.language
        # A generic name avoids disclosing workspace or user filenames upstream.
        suffix = {
            "audio/wav": "wav",
            "audio/mpeg": "mp3",
            "audio/flac": "flac",
            "audio/mp4": "m4a",
            "audio/ogg": "ogg",
            "audio/webm": "webm",
        }[request.media_type]
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            response = await guarded_request(
                client,
                "POST",
                self.endpoint("audio/transcriptions"),
                purpose="speech:transcription",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=data,
                files={"file": (f"audio.{suffix}", request.audio, request.media_type)},
                stream_response=True,
                allow_redirects=False,
                max_response_bytes=min(settings.network_max_response_bytes, 1_000_000),
            )
        response.raise_for_status()
        body = response.json()
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("text"), str)
            or not body["text"].strip()
            or len(body["text"]) > 100_000
        ):
            raise SpeechProviderError("语音服务未返回有效的转写文字")
        return TranscriptionResult(body["text"], self.transcription_model)

    async def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if not self.api_key or not self.synthesis_model or not self.voice:
            raise SpeechProviderError("语音合成服务未配置")
        if (
            not request.text.strip()
            or len(request.text) > 4000
            or request.format not in {"mp3", "wav"}
        ):
            raise SpeechProviderError("语音合成文本或格式无效")
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            response = await guarded_request(
                client,
                "POST",
                self.endpoint("audio/speech"),
                purpose="speech:synthesis",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.synthesis_model,
                    "input": request.text,
                    "voice": self.voice,
                    "response_format": request.format,
                },
                stream_response=True,
                allow_redirects=False,
                max_response_bytes=min(settings.network_max_response_bytes, self.max_audio_bytes),
            )
        response.raise_for_status()
        media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        allowed = {
            "mp3": {"audio/mpeg", "audio/mp3"},
            "wav": {"audio/wav", "audio/x-wav", "audio/wave"},
        }
        if (
            media_type not in {*allowed[request.format], "application/octet-stream"}
            or not response.content
        ):
            raise SpeechProviderError("语音服务未返回所请求格式的音频")
        if not valid_audio(response.content, request.format):
            raise SpeechProviderError("语音服务返回的音频格式与请求不一致")
        return SynthesisResult(
            response.content,
            "audio/wav" if request.format == "wav" else "audio/mpeg",
            self.synthesis_model,
            self.voice,
        )


def valid_audio(content: bytes, suffix: str) -> bool:
    if suffix == "wav":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE"
    if suffix == "mp3":
        return content.startswith(b"ID3") or (
            len(content) >= 2 and content[0] == 255 and content[1] & 224 == 224
        )
    if suffix == "flac":
        return content.startswith(b"fLaC")
    if suffix in {"m4a", "mp4"}:
        return len(content) >= 8 and content[4:8] == b"ftyp"
    if suffix == "ogg":
        return content.startswith(b"OggS")
    if suffix == "webm":
        return content.startswith(b"\x1a\x45\xdf\xa3")
    return False


def speech_adapter() -> SpeechAdapter:
    return OpenAICompatibleSpeechAdapter(
        base_url=settings.speech_base_url,
        api_key=settings.speech_api_key.get_secret_value(),
        transcription_model=settings.speech_transcription_model,
        synthesis_model=settings.speech_synthesis_model,
        voice=settings.speech_voice,
        timeout_seconds=settings.speech_timeout_seconds,
        max_audio_bytes=settings.speech_max_audio_bytes,
    )
