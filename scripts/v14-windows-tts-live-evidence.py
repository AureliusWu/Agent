from __future__ import annotations

"""Collect source-bound, real Windows TTS evidence for v14 A02.

This is an explicitly selected live verifier, not a pytest wrapper.  It calls
the production ``TTSManager`` in an isolated runtime, synthesizes one fixed
public Chinese sentence through the Windows provider, validates the resulting
PCM WAV, and then proves that manager shutdown removes the non-cache audio.

It never plays audio, opens a microphone, calls Ollama, loads MeloTTS, or uses a
network TTS provider.  MeloTTS is inspected only to prove that production code
reports it as ``experimental`` rather than presenting it as a stable release
provider.
"""

import argparse
import audioop
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SIYI_ROOT = ROOT / "siyi"
EVIDENCE_RUNNER = ROOT / "scripts" / "v14-evidence-runner.py"
TARGET_VERSION = "14.0.0"
FIXED_PUBLIC_CHINESE_TEXT = (
    "\u53f8\u5fc6\u6b63\u5728\u8fdb\u884c Windows \u672c\u5730\u4e2d\u6587\u8bed\u97f3\u5408\u6210\u9a8c\u6536\uff0c"
    "\u8fd9\u6bb5\u58f0\u97f3\u53ea\u7528\u4e8e\u516c\u5f00\u6d4b\u8bd5\u3002"
)


class WindowsTTSEvidenceError(RuntimeError):
    """A live observation did not meet the bounded A02 contract."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().upper()


def resolve_output_path(value: str, *, repository_root: Path = ROOT) -> Path:
    root = repository_root.resolve()
    evidence_root = (root / "build" / "v1400-evidence").resolve()
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(evidence_root)
    except ValueError as exc:
        raise WindowsTTSEvidenceError("--output must stay under build/v1400-evidence") from exc
    if resolved.suffix.casefold() != ".json":
        raise WindowsTTSEvidenceError("--output must end in .json")
    return resolved


def write_json_immutable(path: Path, payload: dict[str, Any]) -> None:
    """Create an immutable-at-write evidence artifact and flush it to disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileExistsError as exc:
        raise WindowsTTSEvidenceError(f"refusing to overwrite existing evidence: {path.name}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def source_identity() -> dict[str, Any]:
    supplied = os.environ.get("SIYI_V14_EVIDENCE_SOURCE_IDENTITY")
    if supplied:
        try:
            identity = json.loads(supplied)
        except json.JSONDecodeError as exc:
            raise WindowsTTSEvidenceError("runner supplied an invalid source identity") from exc
        required = ("source_version", "source_commit", "source_tree_fingerprint")
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("workspace_clean"), bool)
            or not all(isinstance(identity.get(field), str) and identity[field] for field in required)
        ):
            raise WindowsTTSEvidenceError("runner supplied an incomplete source identity")
        return {field: identity[field] for field in (*required, "workspace_clean")}
    try:
        specification = importlib.util.spec_from_file_location(
            "v14_evidence_runner_source_identity", EVIDENCE_RUNNER
        )
        if specification is None or specification.loader is None:
            raise WindowsTTSEvidenceError("could not load the v14 evidence runner identity helper")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        identity = module.source_identity(ROOT)
    except (OSError, ValueError) as exc:
        raise WindowsTTSEvidenceError("could not calculate the current source identity") from exc
    return dict(identity)


def inspect_pcm_wave(path: Path) -> dict[str, Any]:
    """Return auditable WAV facts and reject non-RIFF/non-PCM/empty output."""

    if not path.is_file():
        raise WindowsTTSEvidenceError("Windows TTS did not create a WAV file")
    header = path.read_bytes()[:12]
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise WindowsTTSEvidenceError("Windows TTS output is not a RIFF/WAVE file")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            compression = handle.getcomptype()
            frames = handle.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise WindowsTTSEvidenceError("Windows TTS output is not a readable PCM WAV") from exc
    if (
        compression != "NONE"
        or channels <= 0
        or sample_width not in {1, 2, 3, 4}
        or sample_rate <= 0
        or frame_count <= 0
        or not frames
    ):
        raise WindowsTTSEvidenceError("Windows TTS output has no valid PCM frames")
    duration_ms = round(frame_count / sample_rate * 1000)
    pcm_rms = int(audioop.rms(frames, sample_width))
    if duration_ms <= 0 or pcm_rms <= 0:
        raise WindowsTTSEvidenceError("Windows TTS output has zero duration or silent PCM")
    return {
        "container": "RIFF/WAVE",
        "encoding": "PCM",
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate": sample_rate,
        "frame_count": frame_count,
        "duration_ms": duration_ms,
        "size_bytes": path.stat().st_size,
        "pcm_rms": pcm_rms,
        "sha256": sha256(path),
    }


def _record_check(report: dict[str, Any], name: str, passed: bool, **details: Any) -> None:
    report.setdefault("checks", {})[name] = {"passed": bool(passed), **details}
    if not passed:
        raise WindowsTTSEvidenceError(name)


def _load_production_components() -> dict[str, Any]:
    if str(SIYI_ROOT) not in sys.path:
        sys.path.insert(0, str(SIYI_ROOT))
    from app.database import init_db, rows
    from app.tts.manager import TTSManager, create_request
    from app.tts.text_normalizer import normalize_for_speech

    return {
        "init_db": init_db,
        "rows": rows,
        "TTSManager": TTSManager,
        "create_request": create_request,
        "normalize_for_speech": normalize_for_speech,
    }


async def _inspect_tts_manager(
    report: dict[str, Any],
    manager: Any,
) -> dict[str, Any]:
    _record_check(
        report,
        "production_tts_manager_path",
        manager.__class__.__module__ == "app.tts.manager",
        module=manager.__class__.__module__,
    )
    initial_status = manager.status()
    _record_check(
        report,
        "manager_initially_idle",
        initial_status.get("status") == "IDLE"
        and initial_status.get("queue_length") == 0
        and initial_status.get("active_synthesis") == [],
    )
    health = await manager.health()
    providers = {
        str(item.get("provider")): item
        for item in health.get("providers", [])
        if isinstance(item, dict) and item.get("provider")
    }
    windows_health = providers.get("windows", {})
    melotts_health = providers.get("melotts", {})
    _record_check(
        report,
        "windows_tts_stable",
        windows_health.get("status") == "ok"
        and windows_health.get("maturity") == "stable"
        and windows_health.get("device") == "cpu",
    )
    _record_check(
        report,
        "melotts_experimental_non_blocking",
        melotts_health.get("maturity") == "experimental",
        observed_status=melotts_health.get("status"),
        release_gate=False,
    )
    voices = await manager.voices("windows")
    chinese_voices = [
        item
        for item in voices
        if isinstance(item, dict)
        and item.get("enabled", True) is not False
        and str(item.get("culture") or "").casefold() == "zh-cn"
    ]
    _record_check(
        report,
        "enabled_zh_cn_voice_available",
        bool(chinese_voices),
        enabled_zh_cn_voice_count=len(chinese_voices),
    )
    return {
        "initial_status": initial_status,
        "windows_health": windows_health,
        "melotts_health": melotts_health,
        "chinese_voices": chinese_voices,
    }


def _configure_tts_manager(
    report: dict[str, Any],
    manager: Any,
    components: dict[str, Any],
    chinese_voices: list[dict[str, Any]],
) -> tuple[str, Any]:
    selected_voice = str(chinese_voices[0].get("name") or "")
    manager.update_settings(
        {
            "enabled": True,
            "provider": "windows",
            "fallback_provider": "windows",
            "allow_fallback": False,
            "voice": selected_voice,
            "playback_mode": "MANUAL",
            "interrupt_policy": "IMMEDIATE",
            "cache_enabled": False,
        }
    )
    normalized = components["normalize_for_speech"](FIXED_PUBLIC_CHINESE_TEXT)
    _record_check(
        report,
        "fixed_text_is_non_sensitive_chinese",
        bool(normalized.text)
        and normalized.sensitive is False
        and any("\u4e00" <= character <= "\u9fff" for character in normalized.text),
        text_character_count=len(normalized.text),
        fixed_text_sha256=sha256_text(FIXED_PUBLIC_CHINESE_TEXT),
    )
    return selected_voice, normalized


async def _synthesize_tts_sample(
    report: dict[str, Any],
    runtime: Path,
    manager: Any,
    components: dict[str, Any],
    selected_voice: str,
) -> dict[str, Any]:
    request_id = f"a02-{uuid.uuid4().hex}"
    request = components["create_request"](
        {
            "request_id": request_id,
            "idempotency_key": request_id,
            "text": FIXED_PUBLIC_CHINESE_TEXT,
            "voice": selected_voice,
            "cache": False,
            "priority": "NORMAL",
        },
        defaults=manager.settings(),
    )
    report["actual_run"] = True
    synthesis = await manager.synthesize(request)
    audio_path = manager.audio_path(request_id)
    wav = inspect_pcm_wave(audio_path)
    _record_check(
        report,
        "manager_synthesizes_real_non_sensitive_chinese_wav",
        synthesis.get("provider") == "windows"
        and synthesis.get("status") == "READY"
        and synthesis.get("cached") is False
        and int(synthesis.get("duration_ms") or 0) > 0,
    )
    _record_check(
        report,
        "riff_wave_pcm_nonempty_duration",
        wav["container"] == "RIFF/WAVE"
        and wav["encoding"] == "PCM"
        and wav["frame_count"] > 0
        and wav["duration_ms"] > 0
        and wav["size_bytes"] > 44
        and wav["pcm_rms"] > 0,
    )
    _record_check(
        report,
        "audio_stays_in_isolated_temporary_root",
        audio_path.resolve().parent == manager.temp.resolve()
        and runtime.resolve() in audio_path.resolve().parents,
    )
    records = components["rows"](
        "SELECT status,provider,cache_id,duration_ms FROM tts_requests WHERE request_id=?",
        (request_id,),
    )
    record = records[0] if len(records) == 1 else {}
    _record_check(
        report,
        "manager_request_lifecycle_persisted",
        record.get("status") == "READY"
        and record.get("provider") == "windows"
        and record.get("cache_id") is None
        and int(record.get("duration_ms") or 0) > 0,
    )
    after_synthesis = manager.status()
    _record_check(
        report,
        "synthesis_does_not_fake_playback",
        after_synthesis.get("status") == "IDLE"
        and after_synthesis.get("queue_length") == 0
        and after_synthesis.get("active_synthesis") == [],
    )
    return {
        "synthesis": synthesis,
        "wav": wav,
        "record": record,
        "after_synthesis": after_synthesis,
    }


def _record_tts_results(
    report: dict[str, Any],
    observed: dict[str, Any],
    sample: dict[str, Any],
    normalized: Any,
    before_wavs: list[str],
) -> None:
    windows = observed["windows_health"]
    melotts = observed["melotts_health"]
    synthesis = sample["synthesis"]
    record = sample["record"]
    after = sample["after_synthesis"]
    report["target"].update(
        {
            "provider_version": str(windows.get("version") or ""),
            "provider_device": str(windows.get("device") or ""),
            "enabled_zh_cn_voice_count": len(observed["chinese_voices"]),
        }
    )
    report["results"] = {
        "providers": {
            "windows": {
                "provider": "windows",
                **{key: windows.get(key) for key in ("status", "device", "version", "maturity")},
            },
            "melotts": {
                "provider": "melotts",
                **{
                    key: melotts.get(key)
                    for key in ("status", "availability", "device", "version", "maturity")
                },
                "release_gate": False,
            },
        },
        "synthesis": {
            **{
                key: synthesis.get(key)
                for key in ("provider", "status", "cached", "duration_ms", "sample_rate", "synthesis_ms")
            },
            "fixed_text_sha256": sha256_text(FIXED_PUBLIC_CHINESE_TEXT),
            "text_character_count": len(FIXED_PUBLIC_CHINESE_TEXT),
            "sensitive": normalized.sensitive,
        },
        "wav": sample["wav"],
        "lifecycle": {
            "initial_status": observed["initial_status"].get("status"),
            "temporary_wavs_before": before_wavs,
            "request_status_after_synthesis": record.get("status"),
            "manager_status_after_synthesis": after.get("status"),
            "queue_length_after_synthesis": after.get("queue_length"),
        },
    }


async def _shutdown_tts_manager(report: dict[str, Any], manager: Any) -> None:
    shutdown_error: str | None = None
    try:
        await manager.shutdown()
    except Exception as exc:  # Preserve the error class without leaking a local path/message.
        shutdown_error = type(exc).__name__
    residual = sorted(path.name for path in manager.temp.glob("*.wav"))
    final_status = manager.status()
    cleanup_passed = (
        shutdown_error is None
        and residual == []
        and final_status.get("status") == "IDLE"
        and final_status.get("queue_length") == 0
        and final_status.get("active_synthesis") == []
    )
    report.setdefault("checks", {})["manager_shutdown_and_temporary_audio_cleanup"] = {
        "passed": cleanup_passed,
        "shutdown_called": True,
        "residual_wav_count": len(residual),
    }
    report["cleanup"] = {
        "manager_shutdown_called": True,
        "manager_shutdown_error": shutdown_error,
        "manager_status_after_shutdown": final_status.get("status"),
        "queue_length_after_shutdown": final_status.get("queue_length"),
        "active_synthesis_after_shutdown": final_status.get("active_synthesis"),
        "temporary_wavs_after_shutdown": residual,
    }
    report.setdefault("results", {}).setdefault("lifecycle", {}).update(
        {
            "manager_status_after_shutdown": final_status.get("status"),
            "temporary_wavs_after_shutdown": residual,
        }
    )
    if not cleanup_passed:
        report["status"] = "FAIL"


async def _exercise_production_manager(
    report: dict[str, Any], runtime: Path, components: dict[str, Any]
) -> None:
    components["init_db"]()
    manager = components["TTSManager"]()
    manager.temp = runtime / "temp" / "tts"
    manager.temp.mkdir(parents=True, exist_ok=True)
    before_wavs = sorted(path.name for path in manager.temp.glob("*.wav"))
    try:
        observed = await _inspect_tts_manager(report, manager)
        selected_voice, normalized = _configure_tts_manager(
            report,
            manager,
            components,
            observed["chinese_voices"],
        )
        sample = await _synthesize_tts_sample(
            report,
            runtime,
            manager,
            components,
            selected_voice,
        )
        _record_tts_results(
            report,
            observed,
            sample,
            normalized,
            before_wavs,
        )
    finally:
        await _shutdown_tts_manager(report, manager)


def run_live_evidence(output: Path) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    runtime = output.parent / f"{output.stem}-runtime-{run_id[:12]}"
    report: dict[str, Any] = {
        "schema_version": 1,
        "report_type": "v14_windows_tts_live_evidence",
        "producer": "scripts/v14-windows-tts-live-evidence.py",
        "target_version": TARGET_VERSION,
        "recorded_at": utc_now(),
        "status": "FAIL",
        "actual_run": False,
        "source": source_identity(),
        "target": {
            "platform": "windows",
            "os_name": os.name,
            "sys_platform": sys.platform,
            "provider": "windows",
            "provider_maturity": "stable",
            "melotts_maturity": "experimental",
        },
        "scope": {
            "production_manager": "app.tts.manager.TTSManager",
            "tts_provider": "windows",
            "fixed_text_classification": "PUBLIC_NON_SENSITIVE",
            "tts_playback": "NOT_RUN",
            "microphone_capture": "NOT_RUN",
            "ollama_actions": "NONE",
            "melotts_model_actions": "NONE",
            "network_tts_calls": "NONE",
        },
        "runtime": {
            "id": run_id,
            "relative_path": runtime.relative_to(ROOT).as_posix(),
            "database_isolated": True,
            "logs_isolated": True,
            "temporary_audio_isolated": True,
        },
        "checks": {},
        "results": {},
        "limitations": [
            "This source-side live synthesis does not prove desktop renderer playback.",
            "It does not prove microphone, installer, endurance, or audio-device acceptance.",
            "MeloTTS is not loaded or synthesized; it remains Experimental and non-blocking.",
        ],
    }
    if os.name != "nt" or sys.platform != "win32":
        report["checks"]["windows_platform"] = {"passed": False}
        report["error"] = {"type": "WindowsTTSEvidenceError", "message": "requires Windows"}
        report["finished_at"] = utc_now()
        return report
    report["checks"]["windows_platform"] = {"passed": True}
    environment_keys = ("AGENT_DATA_ROOT", "AGENT_DATABASE_PATH", "AGENT_LOG_PATH")
    previous_environment = {key: os.environ.get(key) for key in environment_keys}
    try:
        runtime.mkdir(parents=True, exist_ok=False)
        data = runtime / "data"
        logs = runtime / "logs"
        data.mkdir()
        logs.mkdir()
        os.environ["AGENT_DATA_ROOT"] = str(runtime)
        os.environ["AGENT_DATABASE_PATH"] = str(data / "agent.db")
        os.environ["AGENT_LOG_PATH"] = str(logs / "agent.log")
        components = _load_production_components()
        asyncio.run(_exercise_production_manager(report, runtime, components))
        if all(
            isinstance(value, dict) and value.get("passed") is True
            for value in report["checks"].values()
        ):
            report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        message = (
            str(exc)[:160]
            if isinstance(exc, WindowsTTSEvidenceError)
            else "live Windows TTS execution failed; inspect the isolated runtime logs"
        )
        report["error"] = {"type": type(exc).__name__, "message": message}
    finally:
        for key, previous in previous_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        report["finished_at"] = utc_now()
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect real Windows TTS A02 evidence.")
    parser.add_argument(
        "--output",
        required=True,
        help="Repository-relative JSON path under build/v1400-evidence; never overwritten.",
    )
    parser.add_argument(
        "--execute-live-windows-tts",
        action="store_true",
        help="Explicitly run one non-playing Windows SAPI synthesis through production TTSManager.",
    )
    args = parser.parse_args(argv)
    if not args.execute_live_windows_tts:
        parser.error("--execute-live-windows-tts is required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = resolve_output_path(args.output)
    except WindowsTTSEvidenceError as exc:
        print(f"v14 Windows TTS live evidence failed before execution: {exc}", file=sys.stderr)
        return 2
    if output.exists():
        print(
            f"v14 Windows TTS live evidence failed before execution: refusing to overwrite {output.name}",
            file=sys.stderr,
        )
        return 2
    report = run_live_evidence(output)
    try:
        write_json_immutable(output, report)
    except WindowsTTSEvidenceError as exc:
        print(f"v14 Windows TTS live evidence could not write output: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "actual_run": report["actual_run"],
                "output": output.relative_to(ROOT).as_posix(),
                "tts_playback": "NOT_RUN",
                "microphone_capture": "NOT_RUN",
                "ollama_actions": "NONE",
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
