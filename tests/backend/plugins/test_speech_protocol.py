import asyncio
import gzip
import ipaddress
import json

import httpx
import pytest

from app.providers.speech import (
    OpenAICompatibleSpeechAdapter,
    SpeechProviderError,
    SynthesisRequest,
    TranscriptionRequest,
)
from app.security.network_security import NetworkPolicyError

WAV = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00"


@pytest.fixture
def mock_network(monkeypatch):
    real_client = httpx.AsyncClient

    async def public_host(host, port):
        return (ipaddress.ip_address("8.8.8.8"),)

    monkeypatch.setattr("app.security.network_security._resolve_host", public_host)
    monkeypatch.setattr("app.config.settings.network_allowed_domains", "")

    def install(handler):
        monkeypatch.setattr(
            "app.providers.speech.httpx.AsyncClient",
            lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        )

    return install


def adapter(**kwargs):
    return OpenAICompatibleSpeechAdapter(
        "https://speech.example/v1",
        "test-only-speech-key",
        "stt-test",
        "tts-test",
        "test-voice",
        **kwargs
    )


def test_transcription_sends_real_multipart_without_workspace_filename(mock_network):
    async def handle(request):
        assert str(request.url) == "https://speech.example/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer test-only-speech-key"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        body = await request.aread()
        assert b'filename="audio.wav"' in body and WAV in body
        assert b'name="model"' in body and b"stt-test" in body
        assert b'name="language"' in body and b"zh" in body
        return httpx.Response(200, json={"text": "你好司忆"})

    mock_network(handle)
    result = asyncio.run(adapter().transcribe(TranscriptionRequest(WAV, "audio/wav", "zh")))
    assert result.text == "你好司忆" and result.model == "stt-test"


def test_synthesis_uses_response_format_and_validates_audio(mock_network):
    def handle(request):
        assert str(request.url).endswith("/audio/speech")
        assert json.loads(request.content) == {
            "model": "tts-test",
            "input": "你好",
            "voice": "test-voice",
            "response_format": "wav",
        }
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=WAV)

    mock_network(handle)
    result = asyncio.run(adapter().synthesize(SynthesisRequest("你好", "wav")))
    assert result.audio == WAV and result.media_type == "audio/wav"
    assert "test-only-speech-key" not in repr(adapter())


@pytest.mark.parametrize(
    "status,headers,content",
    [
        (200, {"content-type": "audio/wav"}, b"invalid audio"),
        (200, {"content-type": "text/html"}, WAV),
        (200, {"content-type": "audio/wav"}, b""),
    ],
)
def test_invalid_provider_audio_is_rejected(mock_network, status, headers, content):
    mock_network(lambda request: httpx.Response(status, headers=headers, content=content))
    with pytest.raises(SpeechProviderError):
        asyncio.run(adapter().synthesize(SynthesisRequest("你好", "wav")))


def test_voice_redirect_never_forwards_credentials_or_audio(mock_network):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://other.example/stolen"})

    mock_network(handle)
    with pytest.raises(NetworkPolicyError, match="Redirects are disabled"):
        asyncio.run(adapter().transcribe(TranscriptionRequest(WAV, "audio/wav")))
    assert len(requests) == 1


@pytest.mark.parametrize(
    "base",
    [
        "http://speech.example/v1",
        "https://user:pass@speech.example/v1",
        "https://speech.example/v1?api_key=private",
        "https://speech.example/v1#fragment",
    ],
)
def test_speech_endpoint_rejects_credential_bearing_or_insecure_configuration(base):
    with pytest.raises(SpeechProviderError):
        OpenAICompatibleSpeechAdapter(base, "test-only-speech-key").endpoint("audio/speech")


def test_voice_ssrf_is_denied_before_transport(mock_network):
    mock_network(lambda request: pytest.fail("Private endpoint must not reach transport"))
    local = OpenAICompatibleSpeechAdapter(
        "https://169.254.169.254/v1", "test-only-speech-key", "stt-test"
    )
    with pytest.raises(NetworkPolicyError):
        asyncio.run(local.transcribe(TranscriptionRequest(WAV, "audio/wav")))


@pytest.mark.parametrize("declared", [False, True])
def test_streaming_limit_is_enforced_with_or_without_content_length(mock_network, declared):
    headers = {"content-type": "audio/wav"}
    if declared:
        headers["content-length"] = str(len(WAV))
    mock_network(lambda request: httpx.Response(200, headers=headers, content=WAV))
    with pytest.raises(NetworkPolicyError, match="size limit"):
        asyncio.run(adapter(max_audio_bytes=10).synthesize(SynthesisRequest("你好", "wav")))


def test_streamed_compressed_response_is_not_decoded_twice(mock_network):
    mock_network(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "audio/wav", "content-encoding": "gzip"},
            content=gzip.compress(WAV),
        )
    )
    assert asyncio.run(adapter().synthesize(SynthesisRequest("你好", "wav"))).audio == WAV


def test_transcription_rejects_missing_or_nontext_result(mock_network):
    mock_network(lambda request: httpx.Response(200, json={"text": None}))
    with pytest.raises(SpeechProviderError, match="有效的转写"):
        asyncio.run(adapter().transcribe(TranscriptionRequest(WAV, "audio/wav")))
