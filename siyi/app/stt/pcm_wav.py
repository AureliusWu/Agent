"""PCM WAV decoding for the deliberately narrow local-STT input contract.

The desktop recorder normalizes every microphone capture to a 16 kHz mono
16-bit PCM WAV before it reaches the sidecar.  ``faster-whisper`` accepts a
``numpy.ndarray`` waveform directly, so this module keeps that contract inside
the sidecar instead of carrying PyAV and its broad FFmpeg codec payload only
to decode an already-normalized file.

PyAV remains a transitive package dependency of faster-whisper 1.2.1.
Its package initializer imports ``av`` even when an ndarray is used, however
the actual transcribe path does not touch it for ndarray input.  The small
compatibility module below is installed *only when PyAV is absent* (the frozen
sidecar excludes it).  It makes that upstream import succeed while making any
accidental attempt to use arbitrary-codec decoding fail closed.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .schemas import STTError


PCM_SAMPLE_RATE = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2


class PyAVDecodeUnavailable(RuntimeError):
    """Raised if code bypasses the normalized PCM WAV input boundary."""


class _UnavailablePyAVNamespace:
    def __getattr__(self, _name: str) -> Any:
        raise PyAVDecodeUnavailable(
            "PyAV/FFmpeg decoding is intentionally unavailable; local STT only accepts normalized 16 kHz mono 16-bit PCM WAV"
        )


def _raise_pyav_decode_unavailable(*_args: Any, **_kwargs: Any) -> None:
    raise PyAVDecodeUnavailable(
        "PyAV/FFmpeg decoding is intentionally unavailable; local STT only accepts normalized 16 kHz mono 16-bit PCM WAV"
    )


def install_pcm_wav_pyav_compat(*, force: bool = False) -> bool:
    """Provide the minimum import surface needed by faster-whisper's audio module.

    Returns ``True`` only when a compatibility module was inserted.  Production
    code does not replace a real PyAV installation; ``force`` exists solely for
    the isolated frozen-runtime compatibility test.
    """

    current = sys.modules.get("av")
    if current is not None:
        return bool(getattr(current, "__siyi_pcm_wav_compat__", False))
    if not force:
        try:
            if importlib.util.find_spec("av") is not None:
                return False
        except (ImportError, ValueError):
            # A malformed third-party import spec must not make the package
            # silently fall back to a general-purpose codec path.
            return False

    module = types.ModuleType("av")
    invalid_data_error = type("InvalidDataError", (PyAVDecodeUnavailable,), {})
    module.__dict__.update(
        {
            "__siyi_pcm_wav_compat__": True,
            "__all__": (),
            "open": _raise_pyav_decode_unavailable,
            "audio": _UnavailablePyAVNamespace(),
            "error": types.SimpleNamespace(InvalidDataError=invalid_data_error),
        }
    )
    sys.modules["av"] = module
    return True


def load_normalized_pcm_wav(audio_path: Path) -> np.ndarray:
    """Return a float32 waveform with faster-whisper's PyAV-equivalent scale.

    This is intentionally defensive even though ``TemporaryAudioStore`` has
    already enforced the same format before a worker receives the file.  The
    worker process is a trust boundary and must not revert to a permissive
    decoder if it is invoked with a malformed path or payload.
    """

    try:
        with wave.open(str(audio_path), "rb") as source:
            channels = source.getnchannels()
            sample_rate = source.getframerate()
            sample_width = source.getsampwidth()
            compression = source.getcomptype()
            frame_count = source.getnframes()
            pcm = source.readframes(frame_count)
    except (OSError, wave.Error, EOFError) as exc:
        raise STTError("WAV header is invalid", "STT_INVALID_AUDIO") from exc

    if (channels, sample_rate, sample_width, compression) != (
        PCM_CHANNELS,
        PCM_SAMPLE_RATE,
        PCM_SAMPLE_WIDTH,
        "NONE",
    ):
        raise STTError("Audio must be mono 16 kHz 16-bit PCM WAV", "STT_INVALID_AUDIO")
    expected_bytes = frame_count * PCM_CHANNELS * PCM_SAMPLE_WIDTH
    if frame_count <= 0 or len(pcm) != expected_bytes:
        raise STTError("WAV PCM data is incomplete", "STT_INVALID_AUDIO")

    # This exactly mirrors faster_whisper.audio.decode_audio's s16 -> f32
    # conversion while avoiding its PyAV/FFmpeg file decoder.
    waveform = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    waveform /= 32768.0
    return np.ascontiguousarray(waveform)
