from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.runtime_paths import ensure_runtime_layout, runtime_layout

from .schemas import STTError


_SESSION_ID = re.compile(r"^[a-z0-9-]{16,80}$")
MAX_AUDIO_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class StoredAudio:
    path: Path
    sha256: str
    duration_ms: int
    sample_rate: int
    channels: int
    sample_width: int


class TemporaryAudioStore:
    """Owns the only allowed voice-input directory; callers never select paths."""

    def __init__(self, root: Path | None = None) -> None:
        # Tests and isolated callers must not create or inspect the user's
        # default runtime tree merely to use a supplied temporary root.
        if root is None:
            layout = ensure_runtime_layout(runtime_layout())
            root = layout.root / "voice" / "tmp"
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, voice_session_id: str) -> Path:
        if not _SESSION_ID.fullmatch(voice_session_id):
            raise STTError("Invalid voice session", "VOICE_SESSION_NOT_FOUND")
        target = (self.root / voice_session_id).resolve()
        if target.parent != self.root:
            raise STTError("Voice temporary path escaped its root", "STT_PERMISSION_DENIED")
        target.mkdir(parents=True, exist_ok=True)
        return target

    async def save_upload(self, voice_session_id: str, upload: UploadFile) -> StoredAudio:
        content_type = (upload.content_type or "").lower().split(";", 1)[0].strip()
        if content_type not in {"audio/wav", "audio/wave", "audio/x-wav", "application/octet-stream"}:
            raise STTError("Only normalized WAV audio is accepted", "STT_INVALID_AUDIO")
        data = bytearray()
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_AUDIO_BYTES:
                raise STTError("Recorded audio exceeds the 20 MiB local limit", "RECORDING_TOO_LONG")
        return await asyncio.to_thread(self.save_bytes, voice_session_id, bytes(data))

    def save_bytes(self, voice_session_id: str, data: bytes) -> StoredAudio:
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise STTError("Audio is not a RIFF/WAV recording", "STT_INVALID_AUDIO")
        directory = self.session_dir(voice_session_id)
        temporary = directory / "recording.partial"
        target = directory / "recording.wav"
        temporary.write_bytes(data)
        temporary.replace(target)
        try:
            with wave.open(str(target), "rb") as source:
                channels = source.getnchannels()
                sample_rate = source.getframerate()
                sample_width = source.getsampwidth()
                compression = source.getcomptype()
                frames = source.getnframes()
                pcm = source.readframes(frames)
        except (wave.Error, EOFError) as exc:
            target.unlink(missing_ok=True)
            raise STTError("WAV header is invalid", "STT_INVALID_AUDIO") from exc
        if channels != 1 or sample_rate != 16_000 or sample_width != 2 or compression != "NONE":
            target.unlink(missing_ok=True)
            raise STTError("Audio must be mono 16 kHz 16-bit PCM WAV", "STT_INVALID_AUDIO")
        duration_ms = round(frames / sample_rate * 1000)
        if duration_ms < 300:
            target.unlink(missing_ok=True)
            raise STTError("Recording is shorter than 300 ms", "RECORDING_TOO_SHORT")
        if duration_ms > 120_000:
            target.unlink(missing_ok=True)
            raise STTError("Recording is longer than 120 seconds", "RECORDING_TOO_LONG")
        # A constant all-zero recording never reaches a model or a message queue.
        if not pcm or not any(pcm):
            target.unlink(missing_ok=True)
            raise STTError("No audible speech was captured", "STT_NO_SPEECH")
        return StoredAudio(
            path=target,
            sha256=hashlib.sha256(data).hexdigest(),
            duration_ms=duration_ms,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
        )

    def delete(self, voice_session_id: str) -> None:
        if not _SESSION_ID.fullmatch(voice_session_id):
            raise STTError("Invalid voice session", "VOICE_SESSION_NOT_FOUND")
        path = (self.root / voice_session_id).resolve()
        if path.parent != self.root:
            raise STTError("Voice temporary path escaped its root", "STT_PERMISSION_DENIED")
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def cleanup_expired(self, max_age_seconds: int = 3600) -> int:
        """Remove only expired, owned session directories.

        This cleanup runs during shutdown and recovery, when it is especially
        important not to turn a stale child name into a recursive delete outside
        the managed temporary-audio root.  A symlink, junction, or other Windows
        reparse point is never an owned session directory, even when its name
        matches the session-id format.
        """

        cutoff = time.time() - max_age_seconds
        removed = 0
        for child in self.root.iterdir():
            owned = self._owned_session_directory(child)
            if owned is None:
                continue
            try:
                expired = owned.stat().st_mtime < cutoff
            except OSError:
                continue
            if expired:
                shutil.rmtree(owned, ignore_errors=True)
                removed += 1
        return removed

    def _owned_session_directory(self, child: Path) -> Path | None:
        """Return a real direct session directory, never a link/reparse point."""

        if child.parent != self.root or not _SESSION_ID.fullmatch(child.name):
            return None
        try:
            metadata = child.lstat()
        except OSError:
            return None
        reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_point):
            return None
        if not stat.S_ISDIR(metadata.st_mode):
            return None
        try:
            resolved = child.resolve(strict=True)
        except OSError:
            return None
        # Resolve after the lstat check as a second ownership guard: an
        # existing junction is resolved outside ``self.root`` and is refused.
        if resolved.parent != self.root:
            return None
        try:
            verified = resolved.lstat()
        except OSError:
            return None
        verified_attributes = int(getattr(verified, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(verified.st_mode) or bool(verified_attributes & reparse_point):
            return None
        return resolved if stat.S_ISDIR(verified.st_mode) else None
