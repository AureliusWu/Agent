# Voice Provider Plugin Prototype

This prototype adds a provider boundary for speech-to-text (STT) and text-to-speech (TTS) without coupling audio vendors to Agent Core.

## Why this is not a normal Extension SDK package

The current declarative Extension SDK intentionally does not execute third-party Python, JavaScript, DLLs, or lifecycle hooks. Real STT/TTS implementations need executable provider code and sometimes native/model dependencies, so voice must use a dedicated provider contract rather than bypassing the extension security model.

## Prototype architecture

```text
Microphone
  -> desktop/frontend/src/voice/recorder.ts
  -> POST /api/voice/transcribe
  -> VoiceService
  -> STTProvider
  -> text
  -> existing Agent runtime (next integration step)

Agent reply text
  -> POST /api/voice/synthesize
  -> VoiceService
  -> TTSProvider
  -> audio
  -> desktop/frontend/src/voice/audioPlayer.ts
  -> speaker
```

## Included now

- `STTProvider` and `TTSProvider` contracts.
- `VoiceProviderRegistry` with explicit provider IDs and duplicate protection.
- `VoiceService` that selects providers without knowing vendor details.
- Mock STT/TTS providers for deterministic tests and API smoke tests.
- `/api/voice/health`, `/api/voice/transcribe`, and `/api/voice/synthesize`.
- Browser/Tauri microphone recorder, API client, and audio player primitives.
- Backend tests for registry, mock transcription, WAV synthesis, and health reporting.

The mock TTS generates a valid silent WAV container. The mock STT only decodes `text/plain` fixtures; real microphone audio deliberately returns a mock marker. This prevents the prototype from pretending that production recognition already exists.

## Provider shape

A real provider only needs to implement one contract:

```python
class FasterWhisperProvider:
    provider_id = "faster-whisper"

    async def health(self) -> dict[str, object]:
        ...

    async def transcribe(self, audio: AudioInput) -> Transcript:
        ...
```

or:

```python
class DoubaoTTSProvider:
    provider_id = "doubao-tts"

    async def health(self) -> dict[str, object]:
        ...

    async def synthesize(self, request: SpeechRequest) -> SpeechOutput:
        ...
```

Then register the provider in the service factory.

## Next implementation slice

1. Add `FasterWhisperProvider` behind an optional dependency and local model setting.
2. Add one cloud TTS provider (Doubao is the preferred Chinese-first candidate).
3. Store provider credentials through the existing credential/secret path; never in manifests.
4. Wire the recorder into `Composer` as push-to-talk.
5. Feed transcript text into the existing Agent send path; voice remains an I/O layer.
6. Add cancellation, request IDs, size/time limits, and streaming later.
7. Only after V1 is stable, add VAD, barge-in, streaming STT/TTS, and full-duplex experiments.

## Security boundary

- Voice provider code does not receive filesystem/process permissions by default.
- Cloud providers should receive only the audio/text payload needed for the selected operation.
- Credentials stay in the existing secret store and must not be embedded in extension manifests.
- The existing local API token still protects the new HTTP routes.
- A production cloud provider should record outbound data-flow/audit events before transmission.
