from __future__ import annotations

"""Collect bounded A24 privacy evidence through the current local STT API.

This is an operator-invoked release-evidence script.  It starts one isolated,
loopback source sidecar and sends a fixed public synthetic Chinese WAV to the
real ``POST /api/stt/transcribe`` route.  It is deliberately *not* a microphone
test: it neither enumerates nor opens a microphone, and a successful result
must never be used to mark microphone privacy or desktop capture acceptance as
passed.

The script also deliberately never calls Ollama, a model-download endpoint, or
a model-delete endpoint.  It links an already installed local ``base`` model
into an isolated runtime, performs one local transcription, exports diagnostics,
then fails closed if raw test text, raw audio marker bytes, or an absolute path
is found in the isolated SQLite files, logs, temporary-audio tree, or diagnostic
ZIP contents.  Its report contains only hashes, lengths, statuses, and redacted
paths; it never writes the source phrase, transcript, WAV bytes, or an absolute
local path to evidence.

Typical invocation (normally wrapped by ``v14-evidence-runner.py``)::

    .\\siyi\\.venv\\Scripts\\python.exe scripts/v14-stt-privacy-live-evidence.py \
      --output build/v1400-evidence/raw/a24-stt-privacy-live.json

The command exits non-zero for any missing prerequisite or privacy finding.  It
does not make a failed environment look like a skipped or passing privacy gate.
"""

import argparse
import audioop
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
import wave
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import httpx


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / "build" / "v1400-evidence"
EVIDENCE_RUNNER = ROOT / "scripts" / "v14-evidence-runner.py"
PYTHON = ROOT / "siyi" / ".venv" / "Scripts" / "python.exe"
SERVER = ROOT / "siyi" / "run_server.py"
STT_LIVE_EVIDENCE_SCRIPT = ROOT / "scripts" / "v14-stt-live-evidence.py"
MODEL_ID = "base"
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
SYNTHETIC_PUBLIC_TEXT = "司忆本地隐私验证使用公开合成中文测试音频，不包含私人对话内容。"
ABSOLUTE_PATH_PATTERNS = (
    re.compile(rb"(?i)(?<![A-Z0-9_])[A-Z]:[\\/][^\r\n\t<>|]+"),
    re.compile(rb"\\\\[^\\/\r\n]+[\\/][^\r\n\t<>|]+"),
    re.compile(rb"(?<![\w:])/(?:home|Users|var|tmp|opt)/[^\r\n\t<>|]+"),
)

_SIDECAR_CLEANUP_SUPPORT: Any | None = None


class PrivacyEvidenceError(RuntimeError):
    """A bounded prerequisite or privacy invariant was not satisfied."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ScanFinding:
    kind: str
    target: Path
    source: str
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class SyntheticTranscription:
    """Privacy-safe metadata retained after the local HTTP response is discarded."""

    text_probes: tuple[bytes, ...]
    audio_markers: tuple[bytes, ...]
    metadata: dict[str, Any]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_identity() -> dict[str, Any]:
    """Record the same identity an attesting runner binds into its envelope."""

    supplied = os.environ.get("SIYI_V14_EVIDENCE_SOURCE_IDENTITY")
    if supplied:
        try:
            identity = json.loads(supplied)
        except json.JSONDecodeError as exc:
            raise PrivacyEvidenceError("INVALID_RUNNER_SOURCE_IDENTITY") from exc
        if not isinstance(identity, dict) or not isinstance(identity.get("workspace_clean"), bool):
            raise PrivacyEvidenceError("INCOMPLETE_RUNNER_SOURCE_IDENTITY")
        required = ("source_version", "source_commit", "source_tree_fingerprint")
        if not all(isinstance(identity.get(field), str) and identity[field] for field in required):
            raise PrivacyEvidenceError("INCOMPLETE_RUNNER_SOURCE_IDENTITY")
        return {field: identity[field] for field in (*required, "workspace_clean")}
    try:
        specification = importlib.util.spec_from_file_location(
            "v14_evidence_runner_source_identity", EVIDENCE_RUNNER
        )
        if specification is None or specification.loader is None:
            raise PrivacyEvidenceError("RUNNER_IDENTITY_HELPER_UNAVAILABLE")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        identity = module.source_identity(ROOT)
    except (OSError, ValueError) as exc:
        raise PrivacyEvidenceError("RUNNER_SOURCE_IDENTITY_UNAVAILABLE") from exc
    return dict(identity)


def formal_models_root() -> Path:
    configured = os.environ.get("LOCALAPPDATA", "").strip()
    local_app_data = Path(configured) if configured else Path.home() / "AppData" / "Local"
    return local_app_data / "AureliusWu" / "Agent" / "voice" / "models"


def redacted_path(path: Path, runtime: Path | None = None) -> str:
    """Produce evidence-safe local path labels without a machine-specific root."""

    resolved = path.resolve()
    if runtime is not None:
        try:
            return "<isolated-runtime>/" + resolved.relative_to(runtime.resolve()).as_posix()
        except ValueError:
            pass
    return "<external-local>/" + (resolved.name or "item")


def resolve_output_path(value: str, *, repository_root: Path = ROOT) -> Path:
    candidate = Path(value)
    root = repository_root.resolve()
    evidence_root = (root / "build" / "v1400-evidence").resolve()
    output = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        output.relative_to(evidence_root)
    except ValueError as exc:
        raise PrivacyEvidenceError("OUTPUT_OUTSIDE_EVIDENCE_ROOT") from exc
    if output.suffix.lower() != ".json":
        raise PrivacyEvidenceError("OUTPUT_NOT_JSON")
    return output


def write_json_once(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileExistsError as exc:
        raise PrivacyEvidenceError("OUTPUT_ALREADY_EXISTS") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Preserve an interrupted filename rather than replacing uncertain raw evidence.
        raise


def isolated_runtime_for(output: Path) -> Path:
    runtime = output.parent / f"{output.stem}-runtime-{uuid.uuid4().hex}"
    try:
        runtime.resolve().relative_to(EVIDENCE_ROOT.resolve())
    except ValueError as exc:
        raise PrivacyEvidenceError("RUNTIME_OUTSIDE_EVIDENCE_ROOT") from exc
    runtime.mkdir(parents=True, exist_ok=False)
    return runtime


def require_installed_model(models_root: Path) -> dict[str, Any]:
    root = models_root.expanduser().resolve()
    model = (root / MODEL_ID).resolve()
    if model.parent != root or not (model / "config.json").is_file():
        raise PrivacyEvidenceError("STT_MODEL_NOT_INSTALLED")
    files = [item for item in model.rglob("*") if item.is_file()]
    return {
        "id": MODEL_ID,
        "installed_before_run": True,
        "directory": "<formal-model-root>/base",
        "config_sha256": sha256_file(model / "config.json"),
        "file_count": len(files),
        "size_bytes": sum(item.stat().st_size for item in files),
    }


def create_models_link(runtime: Path, models_root: Path) -> Path:
    """Make one link inside the isolated runtime; never copy or mutate models."""

    target = models_root.resolve()
    link = runtime / "voice" / "models"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        raise PrivacyEvidenceError("ISOLATED_MODELS_LINK_EXISTS")
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as symlink_error:
        environment = dict(os.environ)
        environment["SIYI_A24_LINK"] = str(link)
        environment["SIYI_A24_TARGET"] = str(target)
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:SIYI_A24_LINK -Target $env:SIYI_A24_TARGET | Out-Null",
            ],
            capture_output=True,
            check=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise PrivacyEvidenceError("ISOLATED_MODELS_LINK_FAILED") from symlink_error
    try:
        if link.resolve() != target:
            raise PrivacyEvidenceError("ISOLATED_MODELS_LINK_TARGET_MISMATCH")
    except OSError as exc:
        raise PrivacyEvidenceError("ISOLATED_MODELS_LINK_UNREADABLE") from exc
    return link


def remove_models_link(link: Path, models_root: Path) -> None:
    """Remove only the link object made in the isolated runtime."""

    if not (link.exists() or link.is_symlink()):
        return
    try:
        if link.resolve() != models_root.resolve():
            raise PrivacyEvidenceError("ISOLATED_MODELS_LINK_TARGET_MISMATCH")
        os.rmdir(link)
    except OSError as exc:
        raise PrivacyEvidenceError("ISOLATED_MODELS_LINK_CLEANUP_FAILED") from exc


def remove_owned_directory(directory: Path, runtime: Path) -> None:
    try:
        directory.resolve().relative_to(runtime.resolve())
    except ValueError as exc:
        raise PrivacyEvidenceError("OWNED_CLEANUP_PATH_ESCAPED") from exc
    if directory.exists():
        shutil.rmtree(directory)


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def isolated_environment(runtime: Path, *, port: int, token: str) -> dict[str, str]:
    """Force all state and providers into a test-only, no-remote configuration."""

    environment = dict(os.environ)
    environment.update(
        {
            "AGENT_DATA_ROOT": str(runtime),
            "AGENT_DATABASE_PATH": str(runtime / "data" / "agent.db"),
            "AGENT_LOG_PATH": str(runtime / "logs" / "agent.log"),
            "AGENT_EXTENSION_DIRECTORY": str(runtime / "extensions"),
            "AGENT_API_TOKEN": token,
            "AGENT_PORT": str(port),
            "AGENT_BIND_HOST": "127.0.0.1",
            "AGENT_MODEL_BASE_URL": "http://127.0.0.1:9",
            "AGENT_VISION_LOCAL_BASE_URL": "http://127.0.0.1:9",
            "AGENT_MODEL_NAME": "offline-a24-no-provider",
            "AGENT_MODEL_ROUTING_ENABLED": "false",
            "AGENT_MODEL_ESCALATION_ENABLED": "false",
            "AGENT_ALLOW_PRIVATE_MODEL_PROVIDER": "false",
            "AGENT_ALLOW_LOCAL_MCP": "false",
            "AGENT_DEEPSEEK_API_KEY": "",
            "AGENT_TAVILY_API_KEY": "",
            "AGENT_BRAVE_API_KEY": "",
        }
    )
    return environment


def load_sidecar_cleanup_support() -> Any:
    """Load the reviewed source-sidecar identity and tree-cleanup primitives.

    The STT live-evidence script already owns the Windows-specific contract for
    matching PID, creation time, executable, and ``run_server.py`` command line
    before it ever invokes ``taskkill /T``.  Reusing that exact implementation
    avoids a second, weaker process-cleanup path in the privacy verifier.
    """

    global _SIDECAR_CLEANUP_SUPPORT
    if _SIDECAR_CLEANUP_SUPPORT is not None:
        return _SIDECAR_CLEANUP_SUPPORT
    if not STT_LIVE_EVIDENCE_SCRIPT.is_file():
        raise PrivacyEvidenceError("SOURCE_SIDECAR_CLEANUP_SUPPORT_MISSING")
    spec = importlib.util.spec_from_file_location(
        "v14_stt_live_evidence_cleanup_support", STT_LIVE_EVIDENCE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise PrivacyEvidenceError("SOURCE_SIDECAR_CLEANUP_SUPPORT_MISSING")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    required = (
        "capture_source_sidecar_identity",
        "stop_source_sidecar",
    )
    if any(not callable(getattr(module, name, None)) for name in required):
        raise PrivacyEvidenceError("SOURCE_SIDECAR_CLEANUP_SUPPORT_MISSING")
    _SIDECAR_CLEANUP_SUPPORT = module
    return module


def _terminate_unverified_launcher(process: subprocess.Popen[bytes]) -> None:
    """Stop only the direct launcher handle before any STT work can begin.

    This fallback is deliberately limited to the just-created ``Popen`` handle.
    Without a captured WMI identity we must not select a PID tree for
    ``taskkill``.  The caller has not made a health or STT API request yet, so
    a Faster-Whisper worker cannot have been requested on this path.
    """

    try:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def start_sidecar(
    runtime: Path, *, port: int, token: str
) -> tuple[subprocess.Popen[bytes], Any, Any, Any]:
    if not PYTHON.is_file() or not SERVER.is_file():
        raise PrivacyEvidenceError("SOURCE_SIDECAR_RUNTIME_MISSING")
    stdout = (runtime / "sidecar.stdout.log").open("wb")
    stderr = (runtime / "sidecar.stderr.log").open("wb")
    try:
        process = subprocess.Popen(
            [str(PYTHON), str(SERVER)],
            cwd=ROOT / "siyi",
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=isolated_environment(runtime, port=port, token=token),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        stdout.close()
        stderr.close()
        raise PrivacyEvidenceError("SOURCE_SIDECAR_START_FAILED") from None
    try:
        identity = load_sidecar_cleanup_support().capture_source_sidecar_identity(process)
    except Exception:
        identity = None
    if identity is None:
        _terminate_unverified_launcher(process)
        stdout.close()
        stderr.close()
        raise PrivacyEvidenceError("SOURCE_SIDECAR_IDENTITY_UNAVAILABLE")
    return process, stdout, stderr, identity


def sidecar_tree_cleanup_succeeded(value: dict[str, Any]) -> bool:
    tree_cleanup = value.get("tree_cleanup")
    return isinstance(tree_cleanup, dict) and tree_cleanup.get("tree_terminated") is True


def stop_sidecar(
    process: subprocess.Popen[bytes] | None,
    stdout: Any | None,
    stderr: Any | None,
    identity: Any | None,
) -> dict[str, Any]:
    """Stop only a re-identified source sidecar tree and close its logs.

    ``stop_source_sidecar`` rechecks WMI identity immediately before its
    PID-targeted tree termination.  If identity is missing, changed, or the
    launcher already exited, it fails closed and this wrapper reports a failed
    cleanup rather than turning an unverified process state into A24 PASS.
    """

    result: dict[str, Any] = {"stop_requested": False, "stopped": False, "forced": False}
    try:
        support = load_sidecar_cleanup_support()
        stopped = support.stop_source_sidecar(process, stderr, identity)
        tree_cleanup = stopped.get("tree_cleanup") if isinstance(stopped, dict) else None
        if not isinstance(tree_cleanup, dict):
            tree_cleanup = {
                "attempted": False,
                "identity_confirmed": False,
                "tree_terminated": False,
                "reason": "source_sidecar_cleanup_result_invalid",
            }
        result["tree_cleanup"] = tree_cleanup
        result["stop_requested"] = bool(tree_cleanup.get("attempted"))
        result["forced"] = bool(tree_cleanup.get("attempted"))
        result["stopped"] = sidecar_tree_cleanup_succeeded(result)
    finally:
        if stdout is not None:
            stdout.close()
        if stderr is not None and not getattr(stderr, "closed", False):
            stderr.close()
    return result


def wait_for_local_health(client: httpx.Client, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = client.get("/api/health")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise PrivacyEvidenceError("SOURCE_SIDECAR_START_TIMEOUT")


def _powershell_synthesize(text: str, output: Path) -> None:
    """Synthesize fixed public text to a file; it does not open a microphone."""

    script = (
        "Add-Type -AssemblyName System.Speech;"
        "[Console]::InputEncoding=[System.Text.Encoding]::UTF8;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$voices=@($s.GetInstalledVoices()|Where-Object{$_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'zh-CN'});"
        "if($voices.Count -lt 1){$s.Dispose();exit 41};"
        "$s.SelectVoice($voices[0].VoiceInfo.Name);"
        "$s.SetOutputToWaveFile($env:SIYI_A24_SYNTHETIC_WAV);"
        "$s.Speak([Console]::In.ReadToEnd());$s.Dispose()"
    )
    environment = dict(os.environ)
    environment["SIYI_A24_SYNTHETIC_WAV"] = str(output)
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        input=text.encode("utf-8"),
        capture_output=True,
        check=False,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 46:
        raise PrivacyEvidenceError("SYNTHETIC_CHINESE_AUDIO_UNAVAILABLE")


def normalize_to_pcm_wav(source: Path, output: Path, *, duration_seconds: int = 5) -> bytes:
    if duration_seconds <= 0:
        raise PrivacyEvidenceError("SYNTHETIC_DURATION_INVALID")
    try:
        with wave.open(str(source), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            compression = handle.getcomptype()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        raise PrivacyEvidenceError("SYNTHETIC_AUDIO_INVALID") from exc
    if compression != "NONE" or not frames or channels not in {1, 2} or sample_width not in {1, 2, 3, 4} or sample_rate <= 0:
        raise PrivacyEvidenceError("SYNTHETIC_AUDIO_INVALID")
    if channels == 2:
        frames = audioop.tomono(frames, sample_width, 0.5, 0.5)
    if sample_width != SAMPLE_WIDTH:
        frames = audioop.lin2lin(frames, sample_width, SAMPLE_WIDTH)
    if sample_rate != SAMPLE_RATE:
        frames, _ = audioop.ratecv(frames, SAMPLE_WIDTH, CHANNELS, sample_rate, SAMPLE_RATE, None)
    target_bytes = duration_seconds * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS
    normalised = (frames * math.ceil(target_bytes / len(frames)))[:target_bytes]
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(normalised)
    return output.read_bytes()


def fixed_audio_markers(audio: bytes, *, marker_size: int = 96, count: int = 4) -> tuple[bytes, ...]:
    """Select deterministic non-silent raw PCM fragments for leakage scans.

    Windows SAPI can place valid speech between leading and trailing silence.
    Select from fixed, non-overlapping PCM-sized blocks rather than assuming
    evenly spaced locations are all non-silent.  This remains fail-closed when
    fewer than ``count`` usable blocks exist.
    """

    frame_bytes = SAMPLE_WIDTH * CHANNELS
    if marker_size <= 0 or count <= 0 or marker_size % frame_bytes:
        raise PrivacyEvidenceError("SYNTHETIC_AUDIO_MARKER_INVALID")
    if len(audio) < 44 + marker_size * count:
        raise PrivacyEvidenceError("SYNTHETIC_AUDIO_TOO_SMALL")
    data_start = 44
    candidates = tuple(
        audio[offset : offset + marker_size]
        for offset in range(data_start, len(audio) - marker_size + 1, marker_size)
        if any(audio[offset : offset + marker_size])
    )
    if len(candidates) < count:
        raise PrivacyEvidenceError("SYNTHETIC_AUDIO_MARKER_INVALID")
    if count == 1:
        markers = (candidates[0],)
    else:
        markers = tuple(candidates[(len(candidates) - 1) * index // (count - 1)] for index in range(count))
    if any(len(marker) != marker_size or not any(marker) for marker in markers):
        raise PrivacyEvidenceError("SYNTHETIC_AUDIO_MARKER_INVALID")
    return markers


def _match_absolute_path(data: bytes) -> bytes | None:
    for pattern in ABSOLUTE_PATH_PATTERNS:
        match = pattern.search(data)
        if match:
            return match.group(0)
    return None


def scan_bytes(
    *,
    target: Path,
    source: str,
    data: bytes,
    text_probes: Iterable[bytes],
    audio_markers: Iterable[bytes],
) -> list[ScanFinding]:
    """Return opaque findings only; caller never serializes leaked content."""

    findings: list[ScanFinding] = []
    for probe in text_probes:
        if probe and probe in data:
            findings.append(ScanFinding("raw_text", target, source, len(probe), sha256_bytes(probe)))
    for marker in audio_markers:
        if marker and marker in data:
            findings.append(ScanFinding("raw_audio", target, source, len(marker), sha256_bytes(marker)))
    absolute = _match_absolute_path(data)
    if absolute is not None:
        findings.append(ScanFinding("absolute_path", target, source, len(absolute), sha256_bytes(absolute)))
    return findings


def scan_regular_file(
    path: Path,
    *,
    source: str,
    text_probes: Iterable[bytes],
    audio_markers: Iterable[bytes],
) -> list[ScanFinding]:
    try:
        return scan_bytes(
            target=path,
            source=source,
            data=path.read_bytes(),
            text_probes=text_probes,
            audio_markers=audio_markers,
        )
    except OSError as exc:
        raise PrivacyEvidenceError("PERSISTED_ARTIFACT_UNREADABLE") from exc


def scan_diagnostic_zip(
    path: Path,
    *,
    text_probes: Iterable[bytes],
    audio_markers: Iterable[bytes],
) -> tuple[list[ScanFinding], int]:
    """Scan both the archive bytes and each uncompressed entry, without names in output."""

    findings = scan_regular_file(
        path,
        source="diagnostic_zip_container",
        text_probes=text_probes,
        audio_markers=audio_markers,
    )
    try:
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
            for name in names:
                if name.startswith("/") or ".." in Path(name).parts:
                    findings.append(ScanFinding("unsafe_diagnostic_entry", path, "diagnostic_zip_entry", len(name.encode("utf-8")), sha256_bytes(name.encode("utf-8"))))
                    continue
                findings.extend(
                    scan_bytes(
                        target=path,
                        source="diagnostic_zip_entry",
                        data=bundle.read(name),
                        text_probes=text_probes,
                        audio_markers=audio_markers,
                    )
                )
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise PrivacyEvidenceError("DIAGNOSTIC_ZIP_UNREADABLE") from exc
    return findings, len(names)


def redacted_findings(findings: Iterable[ScanFinding], runtime: Path) -> list[dict[str, Any]]:
    return [
        {
            "kind": item.kind,
            "target": redacted_path(item.target, runtime),
            "source": item.source,
            "byte_length": item.byte_length,
            "sha256": item.sha256,
        }
        for item in findings
    ]


def current_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(item for item in root.rglob("*") if item.is_file())


def current_entries(root: Path) -> list[Path]:
    return sorted(root.rglob("*")) if root.exists() else []


def http_json(response: httpx.Response, *, required_status: int, error_code: str) -> dict[str, Any]:
    if response.status_code != required_status:
        raise PrivacyEvidenceError(error_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise PrivacyEvidenceError(error_code) from exc
    if not isinstance(payload, dict):
        raise PrivacyEvidenceError(error_code)
    return payload


def apply_cpu_stt_settings(client: httpx.Client) -> None:
    settings = http_json(
        client.put(
            "/api/stt/settings",
            json={
                "enabled": True,
                "provider": "faster_whisper",
                "model_id": MODEL_ID,
                "device": "cpu",
                "compute_type": "int8",
                "vad": True,
                "idle_unload_minutes": 0,
                "gpu_experimental": False,
            },
        ),
        required_status=200,
        error_code="STT_SETTINGS_UPDATE_FAILED",
    )
    if settings.get("device") != "cpu" or settings.get("model_id") != MODEL_ID:
        raise PrivacyEvidenceError("STT_SETTINGS_NOT_APPLIED")


def transcribe_synthetic_public_audio(client: httpx.Client, runtime: Path) -> SyntheticTranscription:
    """Run only the local multipart route, retaining no raw response in evidence."""

    corpus = runtime / "corpus"
    source_wav = corpus / "source.wav"
    upload_wav = corpus / "synthetic-zh.wav"
    try:
        corpus.mkdir(parents=True, exist_ok=False)
        _powershell_synthesize(SYNTHETIC_PUBLIC_TEXT, source_wav)
        audio = normalize_to_pcm_wav(source_wav, upload_wav)
        audio_markers = fixed_audio_markers(audio)
        public_text = SYNTHETIC_PUBLIC_TEXT.encode("utf-8")
        conversation = http_json(
            client.post(
                "/api/conversations",
                json={"title": "A24 synthetic privacy evidence", "workspace": "", "permission_mode": "ask"},
            ),
            required_status=200,
            error_code="CONVERSATION_CREATE_FAILED",
        )
        conversation_id = conversation.get("id")
        if not isinstance(conversation_id, int) or conversation_id < 1:
            raise PrivacyEvidenceError("CONVERSATION_CREATE_FAILED")
        session = http_json(
            client.post("/api/stt/sessions", json={"conversation_id": conversation_id, "device_id": "", "auto_send": False}),
            required_status=201,
            error_code="STT_SESSION_CREATE_FAILED",
        )
        session_id = session.get("voice_session_id")
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{16,80}", session_id):
            raise PrivacyEvidenceError("STT_SESSION_CREATE_FAILED")
        response = client.post(
            "/api/stt/transcribe",
            data={"voice_session_id": session_id},
            files={"file": ("synthetic.wav", audio, "audio/wav")},
        )
        result = http_json(response, required_status=200, error_code="STT_TRANSCRIBE_FAILED")
        transcription = result.get("transcription")
        if not isinstance(transcription, dict):
            raise PrivacyEvidenceError("STT_TRANSCRIBE_FAILED")
        transcript_text = str(transcription.get("text") or "")
        if not transcript_text.strip():
            raise PrivacyEvidenceError("STT_EMPTY_TRANSCRIPT")
        transcript = transcript_text.encode("utf-8")
        metadata = {
            "route": "/api/stt/transcribe",
            "status_code": response.status_code,
            "provider": str(transcription.get("provider") or ""),
            "model": str(transcription.get("model") or ""),
            "transcript_sha256": sha256_bytes(transcript),
            "transcript_length": len(transcript_text),
            "transcript_utf8_bytes": len(transcript),
            "synthetic_public_text": {
                "sha256": sha256_bytes(public_text),
                "length": len(SYNTHETIC_PUBLIC_TEXT),
                "utf8_bytes": len(public_text),
            },
            "synthetic_audio": {
                "sha256": sha256_bytes(audio),
                "bytes": len(audio),
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "sample_width": SAMPLE_WIDTH,
                "duration_ms": 5_000,
                "marker_hashes": [sha256_bytes(marker) for marker in audio_markers],
                "marker_bytes": len(audio_markers[0]),
            },
        }
        if metadata["provider"] != "faster_whisper" or metadata["model"] != MODEL_ID:
            raise PrivacyEvidenceError("STT_UNEXPECTED_PROVIDER")
        return SyntheticTranscription((public_text, transcript), audio_markers, metadata)
    finally:
        remove_owned_directory(corpus, runtime)


def export_diagnostics(client: httpx.Client, runtime: Path) -> list[Path]:
    response = client.post("/api/diagnostics/export")
    if response.status_code != 200:
        raise PrivacyEvidenceError("DIAGNOSTICS_EXPORT_UNAVAILABLE")
    diagnostic_zips = sorted((runtime / "data" / "diagnostics").glob("agent-diagnostics-*.zip"))
    if not diagnostic_zips:
        raise PrivacyEvidenceError("DIAGNOSTIC_ZIP_NOT_CREATED")
    return diagnostic_zips


def unload_stt_model(client: httpx.Client) -> str:
    unload = http_json(
        client.post("/api/stt/models/unload", json={"model_id": MODEL_ID}),
        required_status=200,
        error_code="STT_UNLOAD_FAILED",
    )
    status = str(unload.get("status") or "")
    if status != "UNLOADED":
        raise PrivacyEvidenceError("STT_UNLOAD_FAILED")
    return status


def scan_persisted_artifacts(runtime: Path, run: SyntheticTranscription, diagnostic_zips: Iterable[Path]) -> dict[str, Any]:
    database_files = [runtime / "data" / "agent.db", runtime / "data" / "agent.db-wal", runtime / "data" / "agent.db-shm"]
    log_files = current_files(runtime / "logs") + [item for item in (runtime / "sidecar.stdout.log", runtime / "sidecar.stderr.log") if item.is_file()]
    temporary_root = runtime / "voice" / "tmp"
    temporary_entries = current_entries(temporary_root)
    temporary_files = [path for path in temporary_entries if path.is_file()]
    findings: list[ScanFinding] = []
    scanned: list[dict[str, Any]] = []

    def scan_path(path: Path, category: str, source: str) -> None:
        findings.extend(scan_regular_file(path, source=source, text_probes=run.text_probes, audio_markers=run.audio_markers))
        scanned.append({"category": category, "path": redacted_path(path, runtime), "bytes": path.stat().st_size, "sha256": sha256_file(path)})

    for path in database_files:
        if path.is_file():
            scan_path(path, "sqlite", "sqlite")
    if not scanned:
        raise PrivacyEvidenceError("SQLITE_ARTIFACT_MISSING")
    for path in log_files:
        scan_path(path, "log", "log")
    for path in temporary_files:
        scan_path(path, "temporary_audio", "temporary_audio")
    if temporary_entries:
        findings.append(ScanFinding("temporary_audio_residue", temporary_root, "temporary_audio", len(temporary_entries), sha256_bytes(str(len(temporary_entries)).encode("ascii"))))
    zip_entries = 0
    for path in diagnostic_zips:
        zip_findings, entry_count = scan_diagnostic_zip(path, text_probes=run.text_probes, audio_markers=run.audio_markers)
        findings.extend(zip_findings)
        zip_entries += entry_count
        scanned.append({"category": "diagnostic_zip", "path": redacted_path(path, runtime), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    return {
        "checks": ["raw_text", "raw_audio", "absolute_path", "temporary_audio_residue"],
        "scanned_artifacts": scanned,
        "diagnostic_zip_entry_count": zip_entries,
        "temporary_audio_file_count": len(temporary_files),
        "temporary_audio_entry_count": len(temporary_entries),
        "findings": redacted_findings(findings, runtime),
        "has_findings": bool(findings),
    }


def run_live_evidence(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    """Run one source-sidecar HTTP path and record only privacy-safe metadata."""

    report: dict[str, Any] = {
        "schema_version": 1,
        "report_type": "v14_a24_stt_privacy_live_evidence",
        "producer": "scripts/v14-stt-privacy-live-evidence.py",
        "target_version": "14.0.0",
        "status": "FAIL",
        "actual_run": False,
        "started_at": utc_now(),
        "source": source_identity(),
        "scope": {
            "synthetic_non_microphone_audio": True,
            "microphone_capture": "NOT_RUN",
            "microphone_privacy_proven": False,
            "local_stt_privacy_evidence": "PENDING",
            "ollama_actions": "NONE",
            "model_download": "NOT_CALLED",
            "model_delete": "NOT_CALLED",
            "note": "Synthetic-file evidence supports only local STT persistence/privacy checks; it does not prove microphone or packaged-desktop privacy.",
        },
    }
    runtime: Path | None = None
    models_link: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    stdout: Any | None = None
    stderr: Any | None = None
    sidecar_identity: Any | None = None
    client: httpx.Client | None = None
    error_code: str | None = None
    cleanup: dict[str, Any] = {}
    try:
        if os.name != "nt":
            raise PrivacyEvidenceError("WINDOWS_REQUIRED")
        runtime = isolated_runtime_for(output)
        report["runtime"] = "<isolated-runtime>"
        models_root = args.models_root.expanduser().resolve()
        report["model"] = require_installed_model(models_root)
        models_link = create_models_link(runtime, models_root)
        cleanup["formal_model_linked"] = True

        port = free_loopback_port()
        token = f"v14-a24-{uuid.uuid4().hex}"
        process, stdout, stderr, sidecar_identity = start_sidecar(runtime, port=port, token=token)
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-agent-api-token": token},
            timeout=httpx.Timeout(args.http_timeout_seconds),
        )
        wait_for_local_health(client, timeout_seconds=args.startup_timeout_seconds)

        apply_cpu_stt_settings(client)
        run = transcribe_synthetic_public_audio(client, runtime)
        report["actual_run"] = True
        report["live_stt"] = run.metadata
        cleanup["synthetic_corpus_removed_before_scan"] = True
        diagnostic_zips = export_diagnostics(client, runtime)
        report["live_stt"]["model_unload_status"] = unload_stt_model(client)
        report["scan"] = scan_persisted_artifacts(runtime, run, diagnostic_zips)
        if report["scan"]["has_findings"]:
            raise PrivacyEvidenceError("PERSISTED_PRIVACY_FINDINGS")
        report["status"] = "PASS"
        report["scope"]["local_stt_privacy_evidence"] = "PASS"
    except PrivacyEvidenceError as exc:
        error_code = exc.code
    except Exception:
        error_code = "UNEXPECTED_ERROR"
    finally:
        if client is not None:
            client.close()
        try:
            cleanup["sidecar"] = stop_sidecar(process, stdout, stderr, sidecar_identity)
            if not sidecar_tree_cleanup_succeeded(cleanup["sidecar"]):
                error_code = error_code or "SOURCE_SIDECAR_TREE_CLEANUP_FAILED"
        except Exception:
            cleanup["sidecar"] = {
                "stop_requested": False,
                "stopped": False,
                "forced": False,
                "tree_cleanup": {
                    "attempted": False,
                    "identity_confirmed": False,
                    "tree_terminated": False,
                    "reason": "source_sidecar_cleanup_exception",
                },
            }
            error_code = error_code or "SOURCE_SIDECAR_STOP_FAILED"
        if models_link is not None:
            try:
                remove_models_link(models_link, args.models_root.expanduser().resolve())
                cleanup["formal_model_link_removed"] = True
            except PrivacyEvidenceError as exc:
                cleanup["formal_model_link_removed"] = False
                error_code = error_code or exc.code
        report["cleanup"] = cleanup
        if error_code:
            report["status"] = "FAIL"
            report["error_code"] = error_code
            report["scope"]["local_stt_privacy_evidence"] = "FAIL"
        report["finished_at"] = utc_now()
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded A24 local STT privacy evidence without microphone or network providers.")
    parser.add_argument("--output", required=True, help="JSON evidence path under build/v1400-evidence.")
    parser.add_argument("--models-root", type=Path, default=formal_models_root(), help="Pre-existing formal STT models root; the script never downloads or deletes it.")
    parser.add_argument("--startup-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)
    if args.startup_timeout_seconds <= 0 or args.http_timeout_seconds <= 0:
        parser.error("timeouts must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = resolve_output_path(args.output)
    except PrivacyEvidenceError as exc:
        print(json.dumps({"status": "FAIL", "error_code": exc.code}, ensure_ascii=False))
        return 2
    if output.exists():
        print(json.dumps({"status": "FAIL", "error_code": "OUTPUT_ALREADY_EXISTS"}, ensure_ascii=False))
        return 2
    report = run_live_evidence(args, output)
    try:
        write_json_once(output, report)
    except PrivacyEvidenceError as exc:
        print(json.dumps({"status": "FAIL", "error_code": exc.code}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "actual_run": report["actual_run"],
                "output": output.relative_to(ROOT).as_posix(),
                "microphone_capture": "NOT_RUN",
                "ollama_actions": "NONE",
                "model_download": "NOT_CALLED",
                "model_delete": "NOT_CALLED",
                "error_code": report.get("error_code"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
