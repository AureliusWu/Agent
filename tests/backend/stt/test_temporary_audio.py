from __future__ import annotations

import io
import os
import subprocess
import time
import wave
from pathlib import Path

import pytest

from app.stt.schemas import STTError
from app.stt.temporary_storage import TemporaryAudioStore


def _pcm_wav(*, sample_rate: int = 16_000, channels: int = 1, duration_ms: int = 400) -> bytes:
    """Build a small non-silent PCM WAV fixture without any external recorder."""
    frames = sample_rate * duration_ms // 1000
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x01\x00" * frames * channels)
    return buffer.getvalue()


def test_managed_store_accepts_only_normalized_pcm_wav_and_removes_it(tmp_path: Path) -> None:
    store = TemporaryAudioStore(tmp_path / "voice-tmp")
    session_id = "a" * 32

    stored = store.save_bytes(session_id, _pcm_wav())

    assert stored.path == store.root / session_id / "recording.wav"
    assert stored.path.is_file()
    assert stored.path.parent.parent == store.root
    assert (stored.sample_rate, stored.channels, stored.sample_width, stored.duration_ms) == (16_000, 1, 2, 400)
    assert len(stored.sha256) == 64

    store.delete(session_id)

    assert not (store.root / session_id).exists()


@pytest.mark.parametrize(
    ("make_payload", "code"),
    [
        (lambda: b"not-a-wav", "STT_INVALID_AUDIO"),
        (lambda: _pcm_wav(sample_rate=44_100), "STT_INVALID_AUDIO"),
        (lambda: _pcm_wav(channels=2), "STT_INVALID_AUDIO"),
        (lambda: _pcm_wav(duration_ms=200), "RECORDING_TOO_SHORT"),
    ],
    ids=["not-wav", "wrong-sample-rate", "stereo", "too-short"],
)
def test_managed_store_rejects_uncontrolled_or_noncompliant_audio(tmp_path: Path, make_payload, code: str) -> None:
    store = TemporaryAudioStore(tmp_path / "voice-tmp")
    session_id = "b" * 32

    with pytest.raises(STTError) as raised:
        store.save_bytes(session_id, make_payload())

    assert raised.value.code == code
    assert not (store.root / session_id / "recording.wav").exists()


def test_managed_store_rejects_path_like_session_identifiers(tmp_path: Path) -> None:
    store = TemporaryAudioStore(tmp_path / "voice-tmp")

    with pytest.raises(STTError) as raised:
        store.save_bytes("../outside", _pcm_wav())

    assert raised.value.code == "VOICE_SESSION_NOT_FOUND"


def test_explicit_temporary_root_never_initializes_the_default_runtime_tree(tmp_path: Path, monkeypatch) -> None:
    def unexpected_runtime_layout(*_args, **_kwargs):
        raise AssertionError("explicit test root must not touch the default runtime layout")

    monkeypatch.setattr("app.stt.temporary_storage.ensure_runtime_layout", unexpected_runtime_layout)

    store = TemporaryAudioStore(tmp_path / "isolated-voice")

    assert store.root == (tmp_path / "isolated-voice").resolve()
    assert store.root.is_dir()


def test_expired_cleanup_removes_only_real_expired_session_directories(tmp_path: Path) -> None:
    store = TemporaryAudioStore(tmp_path / "voice-tmp")
    expired_session = store.session_dir("c" * 32)
    (expired_session / "recording.wav").write_bytes(b"synthetic")
    os.utime(expired_session, (time.time() - 60, time.time() - 60))
    non_session_directory = store.root / "not-a-session"
    non_session_directory.mkdir()
    (non_session_directory / "marker.txt").write_text("keep", encoding="utf-8")

    removed = store.cleanup_expired(0)

    assert removed == 1
    assert not expired_session.exists()
    assert (non_session_directory / "marker.txt").read_text(encoding="utf-8") == "keep"


def test_expired_cleanup_never_follows_a_session_named_directory_link(tmp_path: Path) -> None:
    store = TemporaryAudioStore(tmp_path / "voice-tmp")
    expired_session = store.session_dir("d" * 32)
    (expired_session / "recording.wav").write_bytes(b"synthetic")
    os.utime(expired_session, (time.time() - 60, time.time() - 60))
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "must-not-delete.txt"
    marker.write_text("external user data", encoding="utf-8")
    linked_session = store.root / ("e" * 32)
    try:
        os.symlink(outside, linked_session, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlinks are unavailable in this test environment: {exc}")
        junction = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(linked_session), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip("neither directory symlinks nor test junctions are available")

    removed = store.cleanup_expired(0)

    assert removed == 1
    assert not expired_session.exists()
    assert linked_session.exists()
    assert marker.read_text(encoding="utf-8") == "external user data"
