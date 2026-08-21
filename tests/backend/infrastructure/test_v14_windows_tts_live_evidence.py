from __future__ import annotations

import importlib.util
import io
import json
import sys
import wave
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "v14-windows-tts-live-evidence.py"
SPEC = importlib.util.spec_from_file_location("v14_windows_tts_live_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _pcm_wave(path: Path, *, seconds: float = 0.1, sample_rate: int = 16_000) -> None:
    frame_count = int(seconds * sample_rate)
    frames = bytearray()
    for index in range(frame_count):
        sample = 2_000 if index % 2 else -2_000
        frames.extend(int(sample).to_bytes(2, "little", signed=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))


def test_output_is_bounded_and_written_once(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    evidence = root / "build" / "v1400-evidence"
    evidence.mkdir(parents=True)
    output = MODULE.resolve_output_path(
        "build/v1400-evidence/raw/a02.json", repository_root=root
    )
    assert output == evidence / "raw" / "a02.json"
    with pytest.raises(MODULE.WindowsTTSEvidenceError, match="build/v1400-evidence"):
        MODULE.resolve_output_path("outside.json", repository_root=root)

    MODULE.write_json_immutable(output, {"status": "PASS"})
    with pytest.raises(MODULE.WindowsTTSEvidenceError, match="overwrite"):
        MODULE.write_json_immutable(output, {"status": "FAIL"})
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "PASS"


def test_wave_inspection_requires_real_nonempty_pcm(tmp_path: Path) -> None:
    output = tmp_path / "speech.wav"
    _pcm_wave(output)
    facts = MODULE.inspect_pcm_wave(output)
    assert facts["container"] == "RIFF/WAVE"
    assert facts["encoding"] == "PCM"
    assert facts["frame_count"] == 1_600
    assert facts["duration_ms"] == 100
    assert facts["pcm_rms"] > 0
    assert facts["size_bytes"] > 44

    invalid = tmp_path / "invalid.wav"
    invalid.write_bytes(b"not-a-wave")
    with pytest.raises(MODULE.WindowsTTSEvidenceError, match="RIFF/WAVE"):
        MODULE.inspect_pcm_wave(invalid)


def test_live_selection_is_explicit() -> None:
    with pytest.raises(SystemExit):
        MODULE.parse_args(["--output", "build/v1400-evidence/raw/a02.json"])
    selected = MODULE.parse_args(
        [
            "--output",
            "build/v1400-evidence/raw/a02.json",
            "--execute-live-windows-tts",
        ]
    )
    assert selected.execute_live_windows_tts is True


def test_source_identity_requires_complete_runner_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIYI_V14_EVIDENCE_SOURCE_IDENTITY", json.dumps({"source_version": "13.0.0"}))
    with pytest.raises(MODULE.WindowsTTSEvidenceError, match="incomplete"):
        MODULE.source_identity()
    expected = {
        "source_version": "13.0.0",
        "source_commit": "a" * 40,
        "source_tree_fingerprint": "B" * 64,
        "workspace_clean": False,
    }
    monkeypatch.setenv("SIYI_V14_EVIDENCE_SOURCE_IDENTITY", json.dumps(expected))
    assert MODULE.source_identity() == expected


class _Normalized:
    text = MODULE.FIXED_PUBLIC_CHINESE_TEXT
    sensitive = False


class _FakeManager:
    def __init__(self) -> None:
        from app.tts.manager import TTSManager

        # The verifier intentionally binds to the production module name.  A
        # unit double can exercise the rest without invoking SAPI.
        self.__class__.__module__ = TTSManager.__module__
        self.temp = Path("unused")
        self._audio: dict[str, Path] = {}
        self._records: list[dict[str, object]] = []
        self.shutdown_called = False

    def status(self) -> dict[str, object]:
        return {"status": "IDLE", "active_synthesis": [], "queue_length": 0}

    async def health(self) -> dict[str, object]:
        return {
            "status": "ok",
            "providers": [
                {
                    "provider": "windows",
                    "status": "ok",
                    "device": "cpu",
                    "version": "SAPI.SpVoice",
                    "maturity": "stable",
                },
                {
                    "provider": "melotts",
                    "status": "unavailable",
                    "device": "cpu",
                    "version": "optional-local",
                    "maturity": "experimental",
                },
            ],
        }

    async def voices(self, provider_id: str) -> list[dict[str, object]]:
        assert provider_id == "windows"
        return [{"name": "test-zh", "culture": "zh-CN", "enabled": True, "provider": "windows"}]

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        assert payload["provider"] == "windows"
        return dict(payload)

    def settings(self) -> dict[str, object]:
        return {"provider": "windows", "voice": "test-zh", "sample_rate": 16_000}

    async def synthesize(self, request: object) -> dict[str, object]:
        request_id = str(getattr(request, "request_id"))
        output = self.temp / f"{request_id}-windows.wav"
        _pcm_wave(output)
        self._audio[request_id] = output
        self._records.append(
            {
                "status": "READY",
                "provider": "windows",
                "cache_id": None,
                "duration_ms": 100,
            }
        )
        return {
            "request_id": request_id,
            "provider": "windows",
            "status": "READY",
            "cached": False,
            "duration_ms": 100,
            "sample_rate": 16_000,
            "synthesis_ms": 2.5,
        }

    def audio_path(self, request_id: str) -> Path:
        return self._audio[request_id]

    async def shutdown(self) -> None:
        self.shutdown_called = True
        for path in self._audio.values():
            path.unlink(missing_ok=True)
        self._audio.clear()


class _Request:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id


def test_mocked_manager_run_proves_schema_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = {
        "source_version": "13.0.0",
        "source_commit": "a" * 40,
        "source_tree_fingerprint": "B" * 64,
        "workspace_clean": False,
    }
    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "source_identity", lambda: identity)
    manager = _FakeManager()

    def rows(_query: str, _params: tuple[str]) -> list[dict[str, object]]:
        return list(manager._records)

    monkeypatch.setattr(
        MODULE,
        "_load_production_components",
        lambda: {
            "init_db": lambda: None,
            "rows": rows,
            "TTSManager": lambda: manager,
            "create_request": lambda payload, defaults: _Request(str(payload["request_id"])),
            "normalize_for_speech": lambda _text: _Normalized(),
        },
    )
    monkeypatch.setattr(MODULE.os, "name", "nt")
    monkeypatch.setattr(MODULE.sys, "platform", "win32")
    output = tmp_path / "build" / "v1400-evidence" / "raw" / "a02.json"
    output.parent.mkdir(parents=True)

    report = MODULE.run_live_evidence(output)

    assert report["status"] == "PASS"
    assert report["actual_run"] is True
    assert report["source"] == identity
    assert report["target"]["provider_maturity"] == "stable"
    assert report["target"]["melotts_maturity"] == "experimental"
    assert report["scope"]["tts_playback"] == "NOT_RUN"
    assert report["scope"]["microphone_capture"] == "NOT_RUN"
    assert report["scope"]["ollama_actions"] == "NONE"
    assert all(item["passed"] is True for item in report["checks"].values())
    assert report["results"]["providers"]["melotts"]["release_gate"] is False
    assert report["results"]["wav"]["pcm_rms"] > 0
    assert report["cleanup"]["temporary_wavs_after_shutdown"] == []
    assert manager.shutdown_called is True
