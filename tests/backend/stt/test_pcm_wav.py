from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from app.stt.pcm_wav import load_normalized_pcm_wav
from app.stt.schemas import STTError


def _wav(samples: list[int], *, rate: int = 16_000, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.asarray(samples * channels, dtype="<i2").tobytes())
    return buffer.getvalue()


def test_pcm_wav_decoder_matches_faster_whisper_s16_scaling(tmp_path: Path) -> None:
    path = tmp_path / "recording.wav"
    path.write_bytes(_wav([-32768, 0, 32767]))

    waveform = load_normalized_pcm_wav(path)

    assert waveform.dtype == np.float32
    assert waveform.flags.c_contiguous
    assert waveform.tolist() == pytest.approx([-1.0, 0.0, 32767 / 32768])


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (lambda: _wav([1, 2], rate=44_100), "STT_INVALID_AUDIO"),
        (lambda: _wav([1, 2], channels=2), "STT_INVALID_AUDIO"),
        (lambda: b"RIFF\x00\x00\x00\x00WAVE", "STT_INVALID_AUDIO"),
    ],
)
def test_pcm_wav_decoder_rejects_noncanonical_or_invalid_audio(tmp_path: Path, payload, code: str) -> None:
    path = tmp_path / "recording.wav"
    path.write_bytes(payload())

    with pytest.raises(STTError) as raised:
        load_normalized_pcm_wav(path)

    assert raised.value.code == code


def test_faster_whisper_and_real_vad_import_with_only_pcm_compatibility() -> None:
    """Prove the frozen import boundary without pretending to transcribe speech.

    This runs the actual faster-whisper 1.2.1 and ONNX VAD imports in a clean
    child interpreter after inserting the same no-codec compatibility module a
    package without PyAV uses.  Model inference remains a separate live-model
    acceptance test and is intentionally not simulated here.
    """

    repository = Path(__file__).resolve().parents[3]
    backend = repository / "siyi"
    script = "\n".join(
        [
            "import json, sys",
            "from app.stt.pcm_wav import install_pcm_wav_pyav_compat",
            "assert install_pcm_wav_pyav_compat(force=True)",
            "from faster_whisper import WhisperModel",
            "from faster_whisper.vad import get_vad_model",
            "vad_model = get_vad_model()",
            "print(json.dumps({'model_class': WhisperModel.__name__, 'vad_session': bool(getattr(vad_model, 'session', None)), 'pyav_compat': bool(getattr(sys.modules['av'], '__siyi_pcm_wav_compat__', False))}))",
        ]
    )
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env=environment,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {"model_class": "WhisperModel", "vad_session": True, "pyav_compat": True}
