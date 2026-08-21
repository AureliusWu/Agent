from __future__ import annotations

import importlib.metadata
import importlib.util
import math
import sys
import time
from pathlib import Path
from typing import Any

from app.stt.pcm_wav import install_pcm_wav_pyav_compat, load_normalized_pcm_wav
from app.stt.schemas import STTError, TranscriptionRequest, TranscriptionResult

from .base import STTProvider


class FasterWhisperProvider(STTProvider):
    """CPU-first faster-whisper adapter.  It never downloads a model implicitly."""

    id = "faster_whisper"
    name = "FasterWhisperProvider"
    maturity = "stable"

    def __init__(self) -> None:
        self._model: Any | None = None
        self._model_id: str | None = None
        self._device = "cpu"
        self._compute_type = "int8"

    @staticmethod
    def package_available() -> bool:
        if importlib.util.find_spec("faster_whisper") is not None:
            return True
        # PyiFrozenImporter normally exposes a module spec, but retain a
        # guarded import fallback so the packaged health endpoint never marks
        # a collected provider unavailable solely because distribution
        # metadata/import discovery is different from a venv.
        if getattr(sys, "frozen", False):
            try:
                import faster_whisper  # noqa: F401
            except Exception:
                return False
            return True
        return False

    @staticmethod
    def package_version() -> str | None:
        try:
            return importlib.metadata.version("faster-whisper")
        except importlib.metadata.PackageNotFoundError:
            return None

    def health_check(self) -> dict[str, Any]:
        installed = self.package_available()
        return {
            "provider": self.id,
            "name": self.name,
            "maturity": self.maturity,
            "status": "ok" if installed else "unavailable",
            "version": self.package_version() if installed else None,
            "error_code": None if installed else "STT_PROVIDER_UNAVAILABLE",
            "loaded_model": self._model_id,
            "device": self._device,
            "compute_type": self._compute_type,
        }

    def load_model(self, model_id: str, model_path: Path, *, device: str, compute_type: str) -> dict[str, Any]:
        if not self.package_available():
            raise STTError("Faster-whisper is not installed", "STT_PROVIDER_UNAVAILABLE")
        if device != "cpu" and device != "cuda":
            raise STTError("Unsupported STT device", "STT_MODEL_LOAD_FAILED")
        if not (model_path / "config.json").is_file():
            raise STTError("STT model is not installed locally", "STT_MODEL_MISSING")
        try:
            # faster-whisper 1.2.1 imports PyAV at module-import time even
            # though WhisperModel.transcribe bypasses PyAV for ndarray input.
            # The frozen sidecar supplies a fail-closed import shim and always
            # passes a validated PCM waveform below.
            install_pcm_wav_pyav_compat()
            from faster_whisper import WhisperModel

            started = time.perf_counter()
            self._model = WhisperModel(str(model_path), device=device, compute_type=compute_type, local_files_only=True)
            self._model_id = model_id
            self._device = device
            self._compute_type = compute_type
            return {"status": "READY", "model": model_id, "load_ms": round((time.perf_counter() - started) * 1000, 3), "device": device, "compute_type": compute_type}
        except STTError:
            raise
        except Exception as exc:
            self._model = None
            self._model_id = None
            raise STTError(f"Faster-whisper model load failed: {type(exc).__name__}", "STT_MODEL_LOAD_FAILED") from exc

    def unload_model(self) -> dict[str, Any]:
        model = self._model_id
        self._model = None
        self._model_id = None
        return {"status": "UNLOADED", "model": model}

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        if self._model is None or self._model_id != request.model_id:
            raise STTError("Requested STT model is not loaded", "STT_MODEL_MISSING")
        started = time.perf_counter()
        try:
            waveform = load_normalized_pcm_wav(Path(request.audio_path))
            segments, info = self._model.transcribe(
                waveform,
                language=request.language or None,
                beam_size=5,
                vad_filter=bool(request.vad),
                condition_on_previous_text=False,
                word_timestamps=bool(request.timestamps),
            )
            result_segments = []
            chunks: list[str] = []
            confidence_samples: list[tuple[float, float]] = []
            for segment in segments:
                text = str(getattr(segment, "text", "")).strip()
                if text:
                    chunks.append(text)
                    avg_logprob = _finite_probability_input(getattr(segment, "avg_logprob", None))
                    no_speech_prob = _finite_probability_input(getattr(segment, "no_speech_prob", None))
                    if avg_logprob is not None and no_speech_prob is not None:
                        confidence_samples.append((avg_logprob, no_speech_prob))
                result_segments.append({"start": round(float(getattr(segment, "start", 0)), 3), "end": round(float(getattr(segment, "end", 0)), 3), "text": text})
            text = "".join(chunks).strip()
            duration = max(1, request.audio_duration_ms)
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            if not text:
                raise STTError("No speech was recognized", "STT_NO_SPEECH")
            confidence, reliable, reliability_reason = _transcription_reliability(
                language_probability=getattr(info, "language_probability", None),
                recognized_segment_count=len(chunks),
                samples=confidence_samples,
            )
            return TranscriptionResult(
                request_id=request.request_id,
                provider=self.id,
                model=request.model_id,
                language=str(getattr(info, "language", request.language) or request.language),
                text=text,
                segments=result_segments,
                duration_ms=request.audio_duration_ms,
                transcription_ms=elapsed,
                real_time_factor=round(elapsed / duration, 4),
                confidence=confidence,
                reliable=reliable,
                reliability_reason=reliability_reason,
            )
        except STTError:
            raise
        except Exception as exc:
            raise STTError(f"Faster-whisper transcription failed: {type(exc).__name__}", "STT_TRANSCRIPTION_FAILED") from exc

    def status(self) -> dict[str, Any]:
        return {"provider": self.id, "model": self._model_id, "device": self._device, "compute_type": self._compute_type, "loaded": self._model is not None}


def _finite_probability_input(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _transcription_reliability(
    *,
    language_probability: Any,
    recognized_segment_count: int,
    samples: list[tuple[float, float]],
) -> tuple[float | None, bool, str]:
    """Convert Faster-Whisper's explicit scores into a fail-closed decision."""

    language = _finite_probability_input(language_probability)
    if (
        language is None
        or not 0 <= language <= 1
        or recognized_segment_count <= 0
        or len(samples) != recognized_segment_count
    ):
        return None, False, "CONFIDENCE_UNAVAILABLE"
    average_logprob = sum(item[0] for item in samples) / len(samples)
    maximum_no_speech = max(item[1] for item in samples)
    if not 0 <= maximum_no_speech <= 1:
        return None, False, "CONFIDENCE_UNAVAILABLE"
    token_confidence = math.exp(min(0.0, average_logprob))
    speech_confidence = 1.0 - maximum_no_speech
    confidence = round(max(0.0, min(1.0, language, token_confidence, speech_confidence)), 4)
    reliable = confidence >= 0.60 and language >= 0.70 and maximum_no_speech <= 0.50
    return confidence, reliable, "RELIABLE" if reliable else "LOW_CONFIDENCE"
