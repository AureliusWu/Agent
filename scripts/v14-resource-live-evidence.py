from __future__ import annotations

"""Collect deliberately bounded A20 local-resource evidence for v14.

This is an operator-invoked *live* verifier.  It does not run unless the
caller supplies ``--execute-live-resource-run``.  When selected, it proves one
specific, safe coexistence sequence against the current source tree:

``test-owned managed Ollama qwen3:4b (preloaded) + CPU/int8 Faster-Whisper
small + Windows TTS synthesis``.

Important safety boundaries:

* existing ``qwen3:4b`` and the product-default ``small`` are prerequisites; this script never
  calls a pull/download/delete endpoint and never copies either model;
* port 11434 is always external and outside this run's mutation scope.  This script never preloads,
  unloads, starts or stops it;
* the real preload/unload target is a test-owned managed Ollama listener on
  port 11435.  Its state is bound to an opaque owner hash, PID creation
  identity, executable image and port before any model operation;
* any existing ``ollama.exe`` before the test, or any extra/changed Ollama
  process during it, fails the evidence run rather than being counted or
  terminated;
* a non-empty test-owned ``/api/ps`` rejects the run before preload.  The
  script never evicts a pre-existing/unknown model to make room;
* the qwen model store is an explicitly selected existing dependency used with
  a non-mutating API intent.  This script does not pull, copy, delete or clear
  it, and fingerprints every regular file before and after the run;
* all FastAPI state, database records, logs, synthetic WAVs and TTS temporary
  audio live below the immutable evidence output directory; and
* generated speech is public fixed text synthesized by Windows SAPI.  It is
  explicitly non-microphone test audio and cannot prove microphone/UI gates.

The script is evidence infrastructure, not a release claim.  In particular,
it cannot satisfy the desktop microphone, renderer, endurance, or installer
acceptance items by itself.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


ROOT = Path(__file__).resolve().parents[1]
SIYI_ROOT = ROOT / "siyi"
if str(SIYI_ROOT) not in sys.path:
    sys.path.insert(0, str(SIYI_ROOT))

from app.stt.schemas import DEFAULT_STT_MODEL_ID


EVIDENCE_ROOT = ROOT / "build" / "v1400-evidence"
EVIDENCE_RUNNER = ROOT / "scripts" / "v14-evidence-runner.py"
PYTHON = ROOT / "siyi" / ".venv" / "Scripts" / "python.exe"
STT_SUPPORT_SCRIPT = ROOT / "scripts" / "v14-stt-live-evidence.py"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11435"
QWEN_MODEL = "qwen3:4b"
REQUIRED_A20_STT_MODEL = "small"
STT_MODEL = DEFAULT_STT_MODEL_ID
TEST_SERVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{15,127}$")


class ResourceEvidenceError(RuntimeError):
    """Raised for an observed condition which makes A20 evidence unsafe."""


@dataclass(frozen=True)
class OllamaProcessIdentity:
    """Read-only PID-reuse guard for an external Ollama listener."""

    pid: int
    creation_date: str
    executable_name: str
    command_sha256: str


@dataclass
class A20RunState:
    """Mutable handles for one isolated A20 run and its owner-bound cleanup."""

    arguments: argparse.Namespace
    support: Any
    stt_model_contract: dict[str, Any]
    ollama_url: str
    models_root: Path
    ollama_models_root: Path
    service_id: str
    service_owner_hash: str
    runtime: Path
    sidecar_runtime: Path
    service_state_path: Path
    report: dict[str, Any]
    cleanup: dict[str, Any]
    source_process: subprocess.Popen[bytes] | None = None
    stderr_handle: Any | None = None
    sidecar_identity: Any | None = None
    client: httpx.Client | None = None
    models_link: Path | None = None
    qwen_loaded_by_script: bool = False
    test_owned_service_start_attempted: bool = False
    test_owned_identity: OllamaProcessIdentity | None = None


_SUPPORT_MODULE: Any | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def source_identity() -> dict[str, Any]:
    supplied = os.environ.get("SIYI_V14_EVIDENCE_SOURCE_IDENTITY")
    if supplied:
        try:
            identity = json.loads(supplied)
        except json.JSONDecodeError as exc:
            raise ResourceEvidenceError("runner supplied an invalid source identity") from exc
        if not isinstance(identity, dict) or not isinstance(identity.get("workspace_clean"), bool):
            raise ResourceEvidenceError("runner supplied an incomplete source identity")
        required = ("source_version", "source_commit", "source_tree_fingerprint")
        if not all(isinstance(identity.get(field), str) and identity[field] for field in required):
            raise ResourceEvidenceError("runner supplied an incomplete source identity")
        return {field: identity[field] for field in (*required, "workspace_clean")}
    try:
        specification = importlib.util.spec_from_file_location("v14_evidence_runner_source_identity", EVIDENCE_RUNNER)
        if specification is None or specification.loader is None:
            raise ResourceEvidenceError("could not load the v14 evidence runner identity helper")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        identity = module.source_identity(ROOT)
    except (OSError, ValueError) as exc:
        raise ResourceEvidenceError("could not calculate the current source identity") from exc
    return dict(identity)


def default_models_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data).expanduser() if local_app_data else Path.home() / "AppData" / "Local"
    return base / "AureliusWu" / "Agent" / "voice" / "models"


def default_ollama_models_root() -> Path:
    configured = os.environ.get("OLLAMA_MODELS", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".ollama" / "models"


def resolve_output_path(value: str, *, repository_root: Path = ROOT) -> Path:
    """Permit immutable raw evidence only in the ignored v14 evidence root."""

    root = repository_root.resolve()
    evidence_root = (root / "build" / "v1400-evidence").resolve()
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(evidence_root)
    except ValueError as exc:
        raise ResourceEvidenceError("--output must stay under build/v1400-evidence") from exc
    if resolved.suffix.casefold() != ".json":
        raise ResourceEvidenceError("--output must end in .json")
    return resolved


def write_json_immutable(path: Path, payload: dict[str, Any]) -> None:
    """Create one evidence result without silently replacing an earlier run."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags)
    except FileExistsError as exc:
        raise ResourceEvidenceError(f"refusing to overwrite existing evidence: {path.name}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Keep an incomplete evidence filename rather than allowing a later
        # run to overwrite it.  That makes an interrupted run auditable.
        raise


def validate_ollama_url(value: str) -> str:
    """Accept only the isolated, test-owned loopback lifecycle endpoint."""

    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ResourceEvidenceError("A20 live evidence only accepts loopback HTTP Ollama")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ResourceEvidenceError("A20 live evidence rejects credential-bearing Ollama URLs")
    if parsed.path not in {"", "/"}:
        raise ResourceEvidenceError("A20 live evidence requires the Ollama service root URL")
    if (parsed.port or 11435) != 11435:
        raise ResourceEvidenceError(
            "A20 live evidence only permits the isolated test-owned port 11435; "
            "11434 is external and must remain unmodified"
        )
    return f"http://{parsed.hostname}:11435"


def final_default_stt_model_contract() -> dict[str, str]:
    """Bind A20 to the shipped default and fail closed on any base fallback."""

    if DEFAULT_STT_MODEL_ID != REQUIRED_A20_STT_MODEL or STT_MODEL != DEFAULT_STT_MODEL_ID:
        raise ResourceEvidenceError(
            "A20 requires the product DEFAULT_STT_MODEL_ID to remain small; no base fallback is allowed"
        )
    return {
        "model_id": STT_MODEL,
        "source": "app.stt.schemas.DEFAULT_STT_MODEL_ID",
        "fallback": "DISALLOWED",
        "download_operation": "NOT_CALLED",
    }


def validate_selection(arguments: argparse.Namespace) -> None:
    final_default_stt_model_contract()
    if arguments.startup_timeout_seconds <= 0:
        raise ResourceEvidenceError("--startup-timeout-seconds must be positive")
    if arguments.controller_timeout_seconds <= 0:
        raise ResourceEvidenceError("--controller-timeout-seconds must be positive")
    if not arguments.allow_test_owned_ollama_start:
        raise ResourceEvidenceError(
            "A20 only runs against a test-owned managed service; pass --allow-test-owned-ollama-start"
        )
    if not arguments.test_owned_ollama_service_id or not TEST_SERVICE_ID_PATTERN.fullmatch(
        arguments.test_owned_ollama_service_id
    ):
        raise ResourceEvidenceError(
            "--allow-test-owned-ollama-start requires a 16+ character --test-owned-ollama-service-id"
        )
    if not arguments.ollama_models_root.expanduser().is_dir():
        raise ResourceEvidenceError("--ollama-models-root must name an existing non-mutating dependency directory")
    validate_ollama_url(arguments.ollama_url)


def load_stt_support() -> Any:
    """Load the already-reviewed STT live helper without executing a live run."""

    global _SUPPORT_MODULE
    if _SUPPORT_MODULE is not None:
        return _SUPPORT_MODULE
    if not STT_SUPPORT_SCRIPT.is_file():
        raise ResourceEvidenceError("the v14 STT live-evidence helper is unavailable")
    spec = importlib.util.spec_from_file_location("v14_stt_live_evidence_support", STT_SUPPORT_SCRIPT)
    if spec is None or spec.loader is None:
        raise ResourceEvidenceError("could not load the v14 STT live-evidence helper")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    required = (
        "LiveEvidenceError",
        "api_json",
        "build_synthetic_corpus",
        "create_conversation",
        "create_models_link",
        "free_loopback_port",
        "remove_models_link",
        "run_transcription",
        "start_source_sidecar",
        "stop_source_sidecar",
        "wait_ready",
    )
    if any(not hasattr(module, name) for name in required):
        raise ResourceEvidenceError("the v14 STT live-evidence helper lacks a required safe primitive")
    _SUPPORT_MODULE = module
    return module


def _display_path(path: Path) -> str:
    """Keep release evidence free from user-specific absolute paths."""

    resolved = path.resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        try:
            return "%LOCALAPPDATA%/" + resolved.relative_to(Path(local_app_data).resolve()).as_posix()
        except ValueError:
            pass
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return "<external-local-path>/" + resolved.name


def installed_stt_model_metadata(models_root: Path) -> dict[str, Any]:
    root = models_root.expanduser().resolve()
    model = (root / STT_MODEL).resolve()
    if model.parent != root or not (model / "config.json").is_file():
        raise ResourceEvidenceError("the pre-existing product-default Faster-Whisper small model is unavailable")
    files = [item for item in model.rglob("*") if item.is_file()]
    return {
        "id": STT_MODEL,
        "installed_before_run": True,
        "model_directory": _display_path(model),
        "size_bytes": sum(item.stat().st_size for item in files),
        "file_count": len(files),
        "download_operation": "NOT_CALLED",
        "delete_operation": "NOT_CALLED",
    }


def _identity_from_support(pid: int, support: Any) -> OllamaProcessIdentity:
    raw = support._read_windows_process_identity(pid)
    if raw is None:
        raise ResourceEvidenceError("could not capture the Ollama listener process identity")
    command_line = str(raw.command_line or "")
    return OllamaProcessIdentity(
        pid=int(raw.pid),
        creation_date=str(raw.creation_date),
        executable_name=Path(str(raw.executable_path or "")).name,
        command_sha256=hashlib.sha256(command_line.encode("utf-8", errors="replace")).hexdigest().upper(),
    )


def identity_as_dict(identity: OllamaProcessIdentity) -> dict[str, Any]:
    return {
        "pid": identity.pid,
        "creation_date": identity.creation_date,
        "executable_name": identity.executable_name,
        "command_sha256": identity.command_sha256,
    }


def same_ollama_identity(before: OllamaProcessIdentity, after: OllamaProcessIdentity) -> bool:
    return before == after


def owner_sha256(service_id: str) -> str:
    """Persist/report only an opaque test-owner fingerprint."""

    return hashlib.sha256(service_id.encode("utf-8")).hexdigest()


def safe_service_summary(value: Any) -> dict[str, Any]:
    """Whitelist service fields safe for immutable evidence.

    ``OllamaServiceManager`` deliberately retains private executable/log paths
    internally.  Evidence must not serialize a whole service object because a
    future additive status field could reintroduce those paths.
    """

    service = value if isinstance(value, dict) else {}
    allowed = (
        "api_healthy",
        "base_url",
        "error",
        "latency_ms",
        "listener_pid",
        "managed_pid",
        "managed_port",
        "managed_started_at",
        "managed_state_unowned",
        "mode",
        "owner_sha256",
        "stopped",
        "status",
        "stale_managed_state_recovered",
        "version",
    )
    return {key: service.get(key) for key in allowed if key in service}


def safe_controller_result(value: dict[str, Any]) -> dict[str, Any]:
    """Copy controller output while applying the service-field whitelist."""

    safe = dict(value)
    if "service" in safe:
        safe["service"] = safe_service_summary(safe.get("service"))
    return safe


def successful_test_owned_stop(value: Any) -> bool:
    """Accept only the complete post-stop contract of our owned controller.

    ``OllamaServiceManager.stop`` returns ``stopped=True`` on its service
    object, while the controller separately returns top-level ``ok=True``.
    Both facts, plus an observed non-listening service state, are required.
    This deliberately rejects missing or merely falsy fields so cleanup cannot
    be declared successful after a partial or external-service result.
    """

    controller = value if isinstance(value, dict) else {}
    service = controller.get("service")
    if not isinstance(service, dict):
        return False
    return (
        controller.get("ok") is True
        and controller.get("action") == "stop_test_owned_service"
        and service.get("stopped") is True
        and service.get("status") == "INSTALLED_STOPPED"
        and service.get("api_healthy") is False
        and service.get("mode") is None
        and service.get("managed_pid") is None
        and service.get("listener_pid") is None
        and service.get("managed_port") is None
        and service.get("managed_started_at") is None
        and service.get("owner_sha256") is None
        and service.get("managed_state_unowned") is False
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ollama_model_store_fingerprint(root: Path) -> dict[str, Any]:
    """Fingerprint the complete regular-file tree without following links.

    Passing ``OLLAMA_MODELS`` to a second local service is a non-mutating API
    intent, not an operating-system read-only mount.  Hash every ordinary file
    (manifests and blobs) before and after the run so the evidence detects any
    store mutation.  Links/reparse points are rejected instead of followed;
    otherwise a fingerprint could escape the explicitly selected dependency.
    """

    resolved = root.expanduser().resolve()
    if not resolved.is_dir() or not (resolved / "manifests").is_dir():
        raise ResourceEvidenceError("the explicit Ollama model store has no manifests directory")
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(resolved, followlinks=False):
        current = Path(directory)
        safe_directories: list[str] = []
        for name in sorted(directory_names, key=str.casefold):
            candidate = current / name
            metadata = candidate.lstat()
            attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
            if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_point):
                raise ResourceEvidenceError("the explicit Ollama model store contains a filesystem link")
            if not stat.S_ISDIR(metadata.st_mode):
                raise ResourceEvidenceError("the explicit Ollama model store contains an invalid directory entry")
            safe_directories.append(name)
        directory_names[:] = safe_directories
        for name in sorted(file_names, key=str.casefold):
            candidate = current / name
            metadata = candidate.lstat()
            attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
            if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_point):
                raise ResourceEvidenceError("the explicit Ollama model store contains a filesystem link")
            if not stat.S_ISREG(metadata.st_mode):
                raise ResourceEvidenceError("the explicit Ollama model store contains a non-regular file")
            files.append(candidate)

    digest = hashlib.sha256()
    count = 0
    bytes_total = 0
    for item in sorted(files, key=lambda value: value.relative_to(resolved).as_posix().casefold()):
        relative = item.relative_to(resolved).as_posix()
        before = item.lstat()
        size = before.st_size
        content_sha256 = _hash_file(item)
        after = item.lstat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_mode != after.st_mode
        ):
            raise ResourceEvidenceError("the explicit Ollama model store changed while it was fingerprinted")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(content_sha256.encode("ascii"))
        digest.update(b"\n")
        count += 1
        bytes_total += size
    if count <= 0:
        raise ResourceEvidenceError("the explicit Ollama model store has no regular files")
    return {
        "mode": "non_mutating_api_intent",
        "regular_file_count": count,
        "regular_file_bytes": bytes_total,
        "whole_tree_sha256": digest.hexdigest(),
        "links_followed": False,
    }


def list_ollama_processes() -> list[OllamaProcessIdentity]:
    """Enumerate every Ollama process so resource evidence cannot mix owners."""

    if os.name != "nt":
        raise ResourceEvidenceError("A20 live resource evidence requires Windows process identity inspection")
    script = (
        "$items=@(Get-CimInstance Win32_Process -Filter \"Name='ollama.exe'\" | "
        "Select-Object ProcessId,CreationDate,ExecutablePath,CommandLine);"
        "$items | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise ResourceEvidenceError("could not enumerate Ollama process ownership")
    raw_text = result.stdout.strip()
    if not raw_text:
        return []
    try:
        payload = json.loads(raw_text)
    except ValueError as exc:
        raise ResourceEvidenceError("could not parse Ollama process ownership") from exc
    rows = payload if isinstance(payload, list) else [payload]
    identities: list[OllamaProcessIdentity] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ResourceEvidenceError("Ollama process ownership response was invalid")
        try:
            pid = int(row.get("ProcessId") or 0)
            creation = str(row.get("CreationDate") or "")
            executable = Path(str(row.get("ExecutablePath") or ""))
            command = str(row.get("CommandLine") or "")
        except (TypeError, ValueError) as exc:
            raise ResourceEvidenceError("Ollama process ownership fields were invalid") from exc
        if pid <= 0 or not creation or not executable.name:
            raise ResourceEvidenceError("Ollama process ownership fields were incomplete")
        identities.append(
            OllamaProcessIdentity(
                pid=pid,
                creation_date=creation,
                executable_name=executable.name,
                command_sha256=hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest().upper(),
            )
        )
    return sorted(identities, key=lambda item: item.pid)


def require_only_test_owned_ollama(
    report: dict[str, Any],
    *,
    expected: OllamaProcessIdentity | None,
    phase: str,
) -> None:
    observed = list_ollama_processes()
    passed = expected is not None and observed == [expected]
    require(
        report,
        f"only_test_owned_ollama_{phase}",
        passed,
        observed=[identity_as_dict(item) for item in observed],
        expected=identity_as_dict(expected) if expected else None,
        policy="any additional or changed ollama.exe fails rather than being sampled or terminated",
    )


_MODEL_CONTROLLER = r'''
import asyncio
import json
import os
import sys
from pathlib import Path

payload = json.loads(os.environ["SIYI_V14_RESOURCE_CONTROLLER"])
sys.path.insert(0, payload["siyi_root"])

from app.local_runtime.model_manager import ModelManager, ModelManagerError
from app.local_runtime.ollama_service_manager import OllamaServiceError, OllamaServiceManager
from app.local_runtime.resource_coordinator import ResourceCoordinator
from app.database import init_db

MODEL = "qwen3:4b"

def result(**values):
    print(json.dumps(values, ensure_ascii=False, sort_keys=True))

def owner_matches(service):
    return (
        service.get("status") == "MANAGED_RUNNING"
        and service.get("mode") == "managed"
        and service.get("owner_sha256") == payload["owner_sha256"]
        and int(service.get("managed_pid") or 0) > 0
        and int(service.get("managed_port") or 0) == payload["port"]
        and int(service.get("listener_pid") or 0) == int(service.get("managed_pid") or 0)
    )

async def managed_service():
    lifecycle = OllamaServiceManager(
        base_url=payload["base_url"],
        state_path=Path(payload["state_path"]),
        owner_token=payload["owner_token"],
        require_owner=True,
    )
    service = await lifecycle.status()
    if not owner_matches(service):
        result(ok=False, action=payload["action"], code="TEST_OWNED_SERVICE_BINDING_REQUIRED", service=service)
        return None, None
    return lifecycle, service

async def run():
    action = payload["action"]
    base_url = payload["base_url"]
    if action == "service_status":
        manager = OllamaServiceManager(
            base_url=base_url,
            state_path=Path(payload["state_path"]),
            owner_token=payload["owner_token"],
            require_owner=True,
        )
        result(ok=True, action=action, service=await manager.status())
        return
    if action == "start_test_owned_service":
        manager = OllamaServiceManager(
            base_url=base_url,
            state_path=Path(payload["state_path"]),
            owner_token=payload["owner_token"],
            require_owner=True,
        )
        started = await manager.start(
            executable=payload.get("executable") or None,
            timeout_seconds=float(payload["timeout_seconds"]),
            model_store=Path(payload["model_store"]),
            # The production flag records lifecycle intent but does not impose
            # a filesystem ACL.  The outer verifier therefore hashes the full
            # regular-file tree before and after this service lifetime.
            read_only_model_store=True,
        )
        result(ok=True, action=action, service=started)
        return
    if action == "stop_test_owned_service":
        manager, service = await managed_service()
        if manager is None:
            return
        stopped = await manager.stop()
        result(ok=True, action=action, service=stopped)
        return

    # The controller is intentionally outside the FastAPI lifespan, so it
    # must initialize its own explicitly isolated database before
    # ModelManager records the observed model registry or load lifecycle.
    # controller_environment() binds this process to a disposable runtime.
    init_db()
    manager = ModelManager(base_url)
    coordinator = ResourceCoordinator()
    if action == "preflight":
        _, service = await managed_service()
        if service is None:
            return
        running = await manager.running_models()
        installed = await manager.list_models()
        snapshot = coordinator.snapshot(ollama_pid=int(service["listener_pid"]))
        result(
            ok=True,
            action=action,
            service=service,
            running=running,
            installed=[{"name": item.get("name"), "size": item.get("size"), "loaded": item.get("loaded")} for item in installed],
            resources={"snapshot": snapshot, "admission": coordinator.admission_status(snapshot=snapshot)},
        )
        return
    if action == "preload_qwen":
        _, service = await managed_service()
        if service is None:
            return
        running_before = await manager.running_models()
        if running_before:
            result(ok=False, action=action, code="OLLAMA_RUNTIME_NOT_IDLE", running_before=running_before)
            return
        installed = await manager.list_models()
        if not any(str(item.get("name") or "") == MODEL for item in installed):
            result(ok=False, action=action, code="MODEL_NOT_INSTALLED", installed=[item.get("name") for item in installed])
            return
        # ModelManager performs a second /api/ps check under its lock and uses
        # require_idle_runtime=True, so a pre-existing model is rejected rather
        # than automatically unloaded.
        loaded = await manager.preload(MODEL, keep_alive="5m", require_idle_runtime=True)
        result(ok=True, action=action, service=service, loaded=loaded, running_after=await manager.running_models())
        return
    if action == "unload_owned_qwen":
        _, service = await managed_service()
        if service is None:
            return
        running_before = await manager.running_models()
        unknown_models = [
            str(item.get("name") or item.get("model") or "")
            for item in running_before
            if str(item.get("name") or item.get("model") or "") not in {"", MODEL}
        ]
        if unknown_models:
            # A separate user/runtime action appeared after the guarded idle
            # preflight.  Do not try to reshuffle resident models merely to
            # make a test cleanup look green; leave all of them untouched.
            result(
                ok=False,
                action=action,
                code="OLLAMA_RUNTIME_CHANGED_UNKNOWN_MODEL",
                running_before=running_before,
                running_after=running_before,
            )
            return
        qwen_running = any(str(item.get("name") or item.get("model") or "") == MODEL for item in running_before)
        if not qwen_running:
            result(ok=True, action=action, status="ALREADY_NOT_LOADED", running_before=running_before, running_after=running_before)
            return
        # This invokes only ModelManager.unload(qwen3:4b).  It does not unload
        # or replace any other model discovered after preflight.
        unloaded = await manager.unload(MODEL)
        result(ok=True, action=action, service=service, unloaded=unloaded, running_before=running_before, running_after=await manager.running_models())
        return
    result(ok=False, action=action, code="UNKNOWN_ACTION")

try:
    asyncio.run(run())
except (ModelManagerError, OllamaServiceError) as exc:
    result(ok=False, action=payload.get("action"), code=getattr(exc, "code", type(exc).__name__), error_type=type(exc).__name__)
except Exception as exc:
    result(ok=False, action=payload.get("action"), code="CONTROLLER_ERROR", error_type=type(exc).__name__)
'''


def controller_environment(runtime: Path, payload: dict[str, Any]) -> dict[str, str]:
    """Build a test-only app runtime for the control subprocess."""

    controller_root = runtime / "model-controller"
    environment = dict(os.environ)
    existing_python_path = environment.get("PYTHONPATH", "")
    environment.update(
        {
            "PYTHONPATH": str(ROOT / "siyi") + (os.pathsep + existing_python_path if existing_python_path else ""),
            "AGENT_DATA_ROOT": str(controller_root),
            "AGENT_DATABASE_PATH": str(controller_root / "data" / "agent.db"),
            "AGENT_LOG_PATH": str(controller_root / "logs" / "agent.log"),
            "AGENT_BIND_HOST": "127.0.0.1",
            "AGENT_DEPLOYMENT_MODE": "desktop_local",
            "AGENT_ALLOW_LOCAL_MCP": "false",
            "AGENT_ALLOW_PRIVATE_MODEL_PROVIDER": "false",
            "SIYI_V14_RESOURCE_CONTROLLER": json.dumps(payload, ensure_ascii=False),
        }
    )
    return environment


def run_model_controller(
    runtime: Path,
    *,
    action: str,
    ollama_url: str,
    timeout_seconds: float,
    service_state_path: Path,
    test_owned_service_id: str,
    model_store: Path,
    executable: str | None = None,
) -> dict[str, Any]:
    """Run a narrow production-code action in an isolated Python process."""

    if action not in {
        "service_status",
        "start_test_owned_service",
        "stop_test_owned_service",
        "preflight",
        "preload_qwen",
        "unload_owned_qwen",
    }:
        raise ResourceEvidenceError("resource controller rejected an unknown action")
    if not PYTHON.is_file():
        raise ResourceEvidenceError("repository Python runtime is unavailable")
    payload = {
        "action": action,
        "base_url": validate_ollama_url(ollama_url),
        "state_path": str(service_state_path),
        "siyi_root": str(ROOT / "siyi"),
        "timeout_seconds": float(timeout_seconds),
        "executable": executable or "",
        "owner_token": test_owned_service_id,
        "owner_sha256": owner_sha256(test_owned_service_id),
        "model_store": str(model_store.expanduser().resolve()),
        "port": 11435,
    }
    result = subprocess.run(
        [str(PYTHON), "-c", _MODEL_CONTROLLER],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=controller_environment(runtime, payload),
        timeout=max(1.0, float(timeout_seconds)) + 10.0,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    decoded: dict[str, Any] | None = None
    for line in reversed(result.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            decoded = candidate
            break
    if decoded is None:
        raise ResourceEvidenceError(
            f"resource controller {action} did not produce a machine-readable result (exit {result.returncode})"
        )
    if str(decoded.get("action") or "") != action:
        raise ResourceEvidenceError("resource controller returned a mismatched action")
    return decoded


def require(report: dict[str, Any], name: str, condition: bool, **details: Any) -> None:
    report.setdefault("checks", {})[name] = {"passed": bool(condition), **details}
    if not condition:
        raise ResourceEvidenceError(name)


def _running_model_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    names: list[str] = []
    for item in values:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("model") or "").strip()
            if name:
                names.append(name)
    return names


def _safe_tts_temp_files(sidecar_runtime: Path) -> list[Path]:
    """Return test-owned temporary WAVs only after a strict path boundary check."""

    temp_root = (sidecar_runtime / "temp" / "tts").resolve()
    try:
        temp_root.relative_to(sidecar_runtime.resolve())
    except ValueError as exc:
        raise ResourceEvidenceError("isolated TTS temporary path escaped the test runtime") from exc
    if not temp_root.exists():
        return []
    return [path for path in temp_root.glob("*.wav") if path.is_file() and path.resolve().parent == temp_root]


def read_tts_queue(client: httpx.Client) -> list[dict[str, Any]]:
    """Read the one TTS endpoint whose documented response is a JSON array.

    The shared STT evidence helper deliberately rejects non-object JSON.  Keep
    that default intact: only this read-only queue route accepts an array, and
    every array item must still be an object before A20 uses it as evidence.
    """

    response = client.get("/api/tts/queue")
    if response.status_code != 200:
        raise ResourceEvidenceError(f"GET /api/tts/queue returned {response.status_code}, expected 200")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ResourceEvidenceError("GET /api/tts/queue did not return JSON") from exc
    if not isinstance(payload, list):
        raise ResourceEvidenceError("GET /api/tts/queue returned a non-array JSON payload")
    if not all(isinstance(item, dict) for item in payload):
        raise ResourceEvidenceError("GET /api/tts/queue returned a malformed queue item")
    return payload


def _read_a20_tts_wav(client: httpx.Client, audio_url: str) -> tuple[Any, int, int, int]:
    response = client.get(audio_url)
    if response.status_code != 200 or len(response.content) <= 46:
        raise ResourceEvidenceError("Windows TTS did not return audible WAV data")
    try:
        import io

        with wave.open(io.BytesIO(response.content), "rb") as handle:
            return response, handle.getnframes(), handle.getframerate(), handle.getnchannels()
    except (wave.Error, EOFError) as exc:
        raise ResourceEvidenceError("Windows TTS audio response was not a readable WAV") from exc


def _settle_a20_tts_playback(
    client: httpx.Client, support: Any, request_id: str
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    playback_started = support.api_json(client, "POST", f"/api/tts/playback/{request_id}/start")
    if playback_started.get("status") != "PLAYING":
        raise ResourceEvidenceError("Windows TTS did not acknowledge playback start")
    playback_completed = support.api_json(client, "POST", f"/api/tts/playback/{request_id}/complete")
    if playback_completed.get("status") != "COMPLETED":
        raise ResourceEvidenceError("Windows TTS did not acknowledge playback completion")
    queue_after = read_tts_queue(client)
    status_after = support.api_json(client, "GET", "/api/tts/status")
    if (
        queue_after
        or status_after.get("status") != "IDLE"
        or int(status_after.get("queue_length") or 0) != 0
        or status_after.get("current") is not None
    ):
        raise ResourceEvidenceError("Windows TTS lifecycle did not converge after completion")
    return playback_started, playback_completed, queue_after, status_after


def _interrupt_a20_tts_request(client: httpx.Client, support: Any, *, task_id: str) -> None:
    interrupted = support.api_json(client, "POST", "/api/tts/interrupt", json={"task_id": task_id})
    if interrupted.get("status") != "CANCELLED":
        raise ResourceEvidenceError("Windows TTS exact-task interruption was not acknowledged")


def exercise_windows_tts_lifecycle(client: httpx.Client, support: Any) -> dict[str, Any]:
    """Exercise one exact non-cache TTS request through its terminal API path.

    A raw ``/synthesize`` request intentionally retains its private WAV until a
    renderer acknowledges a terminal playback state.  A20 must not compensate
    for a skipped acknowledgement by deleting the file from evidence code:
    create a task-scoped ``/speak`` request, inspect its real WAV, then report
    ``start`` and ``complete`` just as the desktop renderer does.  If any
    later step fails, only the generated task id is interrupted; a global
    queue cleanup could hide another request's lifecycle bug.
    """

    settings = support.api_json(
        client,
        "PUT",
        "/api/tts/settings",
        json={
            "enabled": True,
            "provider": "windows",
            "fallback_provider": "windows",
            "allow_fallback": False,
            "cache_enabled": False,
            "playback_mode": "MANUAL",
            "interrupt_policy": "IMMEDIATE",
        },
    )
    request_id = f"v14-a20-{uuid.uuid4().hex}"
    task_id = f"v14-a20-task-{uuid.uuid4().hex}"
    message_id = f"v14-a20-message-{uuid.uuid4().hex}"
    speak_attempted = False
    started = time.perf_counter()
    try:
        # Use the same queue and acknowledgement API contract as the desktop
        # renderer.  ``task_id`` is generated here and used only for this
        # exact request's failure-path interruption.
        speak_attempted = True
        synthesized = support.api_json(
            client,
            "POST",
            "/api/tts/speak",
            json={
                "request_id": request_id,
                "idempotency_key": request_id,
                "task_id": task_id,
                "message_id": message_id,
                "text": "\u53f8\u5fc6\u6b63\u5728\u8fdb\u884c\u672c\u5730\u8d44\u6e90\u534f\u8c03\u6d4b\u8bd5\u3002",
                "cache": False,
                "priority": "LOW",
            },
        )
        wall_ms = round((time.perf_counter() - started) * 1000.0, 3)
        if str(synthesized.get("request_id") or "") != request_id:
            raise ResourceEvidenceError("Windows TTS did not preserve the test-owned request id")
        if synthesized.get("status") != "QUEUED":
            raise ResourceEvidenceError("Windows TTS did not enter the playback queue")
        audio_url = str(synthesized.get("audio_url") or "")
        if not audio_url.startswith(f"/api/tts/audio/{request_id}"):
            raise ResourceEvidenceError("Windows TTS did not return a controlled audio URL")
        response, frames, sample_rate, channels = _read_a20_tts_wav(client, audio_url)
        playback_started, playback_completed, queue_after, status_after = _settle_a20_tts_playback(
            client, support, request_id
        )
    except Exception:
        if speak_attempted:
            try:
                _interrupt_a20_tts_request(client, support, task_id=task_id)
            except Exception as cleanup_exc:
                raise ResourceEvidenceError(
                    "Windows TTS lifecycle failed and exact-task interruption could not be confirmed"
                ) from cleanup_exc
        raise
    return {
        "settings": settings,
        "request_id": request_id,
        "task_id": task_id,
        "message_id": message_id,
        "provider": synthesized.get("provider"),
        "duration_ms": synthesized.get("duration_ms"),
        "synthesis_ms": synthesized.get("synthesis_ms"),
        "wall_ms": wall_ms,
        "cache_hit": synthesized.get("cache_hit"),
        "audio": {
            "http_status": response.status_code,
            "bytes": len(response.content),
            "frames": frames,
            "sample_rate": sample_rate,
            "channels": channels,
            "cache_control": response.headers.get("cache-control"),
        },
        "playback": {
            "queued_status": synthesized.get("status"),
            "started_status": playback_started.get("status"),
            "completed_status": playback_completed.get("status"),
            "queue_after": len(queue_after),
            "status_after": status_after.get("status"),
            "active_synthesis_after": len(status_after.get("active_synthesis") or []),
        },
    }


def _resource_view(client: httpx.Client, support: Any, *, expected_ollama_pid: int) -> dict[str, Any]:
    value = support.api_json(client, "GET", "/api/local-models/resources")
    snapshot = value.get("snapshot") if isinstance(value.get("snapshot"), dict) else {}
    observed_pid = int(value.get("ollama_listener_pid") or 0)
    if observed_pid != expected_ollama_pid or int(snapshot.get("ollama_pid") or 0) != expected_ollama_pid:
        raise ResourceEvidenceError("the source sidecar resource route was not bound to the test-owned Ollama PID")
    return {
        "policy": value.get("policy"),
        "snapshot": snapshot,
        "ollama_listener_pid": observed_pid,
        "admission": value.get("admission"),
        "active_model_requests": value.get("active_model_requests"),
        "tts_status": value.get("tts_status"),
        "stt_status": value.get("stt_status"),
    }


def start_source_sidecar_for_test_owned_ollama(
    support: Any,
    runtime: Path,
    *,
    port: int,
    token: str,
    ollama_url: str,
) -> tuple[subprocess.Popen[bytes], Any, Any]:
    """Start one isolated sidecar whose local-model route targets this test port."""

    previous = os.environ.get("AGENT_LOCAL_OLLAMA_URL")
    os.environ["AGENT_LOCAL_OLLAMA_URL"] = ollama_url
    try:
        return support.start_source_sidecar(runtime, port=port, token=token)
    finally:
        if previous is None:
            os.environ.pop("AGENT_LOCAL_OLLAMA_URL", None)
        else:
            os.environ["AGENT_LOCAL_OLLAMA_URL"] = previous


def _new_a20_run_state(arguments: argparse.Namespace, output: Path) -> A20RunState:
    support = load_stt_support()
    stt_model_contract = final_default_stt_model_contract()
    ollama_url = validate_ollama_url(arguments.ollama_url)
    models_root = arguments.models_root.expanduser().resolve()
    ollama_models_root = arguments.ollama_models_root.expanduser().resolve()
    service_id = str(arguments.test_owned_ollama_service_id)
    service_owner_hash = owner_sha256(service_id)
    run_id = uuid.uuid4().hex
    runtime = output.parent / f"{output.stem}-runtime-{run_id[:12]}"
    report: dict[str, Any] = {
        "schema_version": 1,
        "report_type": "v14_resource_live_evidence",
        "producer": "scripts/v14-resource-live-evidence.py",
        "target_version": "14.0.0",
        "recorded_at": utc_now(),
        "status": "FAIL",
        "actual_run": False,
        "source": source_identity(),
        "scope": {
            "ollama_url": ollama_url,
            "external_11434_policy": "non_mutating_intent_not_used_for_preload_or_unload",
            "test_owned_service_owner_sha256": service_owner_hash,
            "qwen_model": QWEN_MODEL,
            "model_store_policy": "non_mutating_api_intent_full_tree_verified",
            "stt_model": stt_model_contract["model_id"],
            "stt_model_source": stt_model_contract["source"],
            "stt_model_fallback": stt_model_contract["fallback"],
            "stt_device": "cpu",
            "stt_compute_type": "int8",
            "tts_provider": "windows",
            "model_download": "NOT_CALLED",
            "model_delete": "NOT_CALLED",
            "microphone_capture": "NOT_RUN",
            "tts_playback": "NOT_RUN",
            "desktop_renderer": "NOT_RUN",
            "endurance": "NOT_RUN",
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
            "本脚本只验证受控的本地资源共存，不代表麦克风、桌面前端、播放或耐久通过。",
            "STT 语料由 Windows SAPI 生成，属于合成非麦克风测试音频。",
            "qwen3:4b 只执行预加载与卸载，不发送聊天提示词，也不调用付费 Provider。",
            "若外部 Ollama 已有模型常驻，本脚本拒绝执行，不会卸载未知模型。",
        ],
    }
    cleanup: dict[str, Any] = {
        "qwen_loaded_by_script": False,
        "stt_unload": None,
        "qwen_unload": None,
        "sidecar": None,
        "tts_temp_residual_after_lifecycle": [],
        "tts_temp_residual_after_sidecar_stop": [],
        "synthetic_corpus_cleanup": [],
        "formal_models_link_removed": False,
        "test_owned_ollama_start_attempted": False,
        "test_owned_ollama_stop": None,
        "ollama_processes_after_stop": [],
        "ollama_model_store_after": None,
    }
    return A20RunState(
        arguments=arguments,
        support=support,
        stt_model_contract=stt_model_contract,
        ollama_url=ollama_url,
        models_root=models_root,
        ollama_models_root=ollama_models_root,
        service_id=service_id,
        service_owner_hash=service_owner_hash,
        runtime=runtime,
        sidecar_runtime=runtime / "sidecar",
        service_state_path=runtime / "ollama-test-owned-managed.json",
        report=report,
        cleanup=cleanup,
    )


def _a20_owned_identity(state: A20RunState) -> OllamaProcessIdentity:
    if state.test_owned_identity is None:
        raise ResourceEvidenceError("the test-owned Ollama identity is unavailable")
    return state.test_owned_identity


def _a20_controller(
    state: A20RunState,
    *,
    action: str,
    timeout_seconds: float | None = None,
    executable: str | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if action == "start_test_owned_service":
        options["executable"] = executable
    return run_model_controller(
        state.runtime,
        action=action,
        ollama_url=state.ollama_url,
        timeout_seconds=(
            timeout_seconds
            if timeout_seconds is not None
            else state.arguments.controller_timeout_seconds
        ),
        service_state_path=state.service_state_path,
        test_owned_service_id=state.service_id,
        model_store=state.ollama_models_root,
        **options,
    )


def _preflight_a20_resources(state: A20RunState) -> None:
    preflight = _a20_controller(state, action="preflight")
    state.report["results"]["preflight"] = safe_controller_result(preflight)
    running_before = _running_model_names(preflight.get("running"))
    installed_names = _running_model_names(preflight.get("installed"))
    resources = preflight.get("resources")
    admission = resources.get("admission", {}) if isinstance(resources, dict) else {}
    snapshot = resources.get("snapshot", {}) if isinstance(resources, dict) else {}
    required_admissions = ("model_preload", "stt_cpu", "voice")
    require(
        state.report,
        "test_owned_ollama_idle_before_preload",
        not running_before,
        running_models=running_before,
        policy="reject rather than evict any pre-existing test-port model",
    )
    require(
        state.report,
        "preexisting_qwen3_4b",
        QWEN_MODEL in installed_names,
        installed_models=installed_names,
    )
    require(
        state.report,
        "target_hardware_16gb_6gb_observed",
        isinstance(snapshot.get("system_total_bytes"), int)
        and int(snapshot["system_total_bytes"]) >= 15 * 1024**3
        and isinstance(snapshot.get("gpu_total_bytes"), int)
        and int(snapshot["gpu_total_bytes"]) >= 6 * 1024**3,
        system_total_bytes=snapshot.get("system_total_bytes"),
        gpu_total_bytes=snapshot.get("gpu_total_bytes"),
        nominal_target="16GB_RAM_6GB_VRAM",
        windows_usable_ram_floor_bytes=15 * 1024**3,
    )
    require(
        state.report,
        "fresh_resource_admission_before_work",
        all(
            isinstance(admission.get(name), dict) and admission[name].get("allowed") is True
            for name in required_admissions
        ),
        admission=admission,
        required_admissions=list(required_admissions),
    )


def _start_and_preflight_a20_ollama(state: A20RunState) -> None:
    preexisting_ollama = list_ollama_processes()
    require(
        state.report,
        "no_preexisting_ollama_processes",
        not preexisting_ollama,
        observed=[identity_as_dict(item) for item in preexisting_ollama],
        policy="external Ollama, including port 11434, is outside mutation scope and blocks this managed evidence run",
    )
    state.report["results"]["ollama_model_store_before"] = ollama_model_store_fingerprint(
        state.ollama_models_root
    )
    state.report["stt_model"] = installed_stt_model_metadata(state.models_root)
    require(
        state.report,
        "final_default_stt_model_is_small",
        STT_MODEL == REQUIRED_A20_STT_MODEL == DEFAULT_STT_MODEL_ID,
        **state.stt_model_contract,
    )
    require(state.report, "preexisting_formal_stt_small", True, **state.report["stt_model"])

    # A malformed/timeout response can follow a successful process launch, so
    # mark the attempt before the controller call and always run owner-bound stop.
    state.test_owned_service_start_attempted = True
    state.cleanup["test_owned_ollama_start_attempted"] = True
    started = _a20_controller(
        state,
        action="start_test_owned_service",
        timeout_seconds=state.arguments.startup_timeout_seconds,
        executable=state.arguments.test_owned_ollama_executable,
    )
    service = started.get("service") if isinstance(started.get("service"), dict) else {}
    if (
        started.get("ok") is not True
        or service.get("status") != "MANAGED_RUNNING"
        or service.get("mode") != "managed"
        or service.get("owner_sha256") != state.service_owner_hash
        or int(service.get("managed_pid") or 0) != int(service.get("listener_pid") or 0)
        or int(service.get("managed_port") or 0) != 11435
    ):
        raise ResourceEvidenceError(
            "the requested test-owned Ollama service did not prove its complete ownership binding"
        )
    state.test_owned_identity = _identity_from_support(int(service["managed_pid"]), state.support)
    state.report["results"]["ollama_service_before"] = {
        "mode": "test_owned_managed",
        "owner_sha256": state.service_owner_hash,
        "service": safe_service_summary(service),
        "identity": identity_as_dict(state.test_owned_identity),
    }
    require_only_test_owned_ollama(
        state.report, expected=state.test_owned_identity, phase="after_start"
    )
    _preflight_a20_resources(state)


def _prepare_a20_source_sidecar(state: A20RunState) -> dict[str, Any]:
    state.models_link, link_kind = state.support.create_models_link(
        state.sidecar_runtime, state.models_root
    )
    state.report["runtime"]["formal_model_link"] = {
        "kind": link_kind,
        "target": _display_path(state.models_root),
        "copy_performed": False,
    }
    samples, corpus_voice = state.support.build_synthetic_corpus(state.sidecar_runtime)
    state.report["results"]["synthetic_corpus"] = {
        "kind": "synthetic_non_microphone",
        "generator": "Windows SAPI",
        "voice": corpus_voice,
        "microphone_used": False,
        "sample": {
            key: value
            for key, value in samples["chinese_5s"].items()
            if key not in {"path", "reference_text"}
        },
    }
    port = state.support.free_loopback_port()
    token = uuid.uuid4().hex
    state.source_process, state.stderr_handle, state.sidecar_identity = (
        start_source_sidecar_for_test_owned_ollama(
            state.support,
            state.sidecar_runtime,
            port=port,
            token=token,
            ollama_url=state.ollama_url,
        )
    )
    state.client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"X-Agent-Api-Token": token},
        timeout=httpx.Timeout(45.0),
    )
    health = state.support.wait_ready(
        state.client,
        state.source_process,
        timeout_seconds=state.arguments.startup_timeout_seconds,
    )
    require(state.report, "isolated_source_sidecar_ready", health.get("status") == "ok", health=health)
    state.report["results"]["resources_before_preload"] = _resource_view(
        state.client, state.support, expected_ollama_pid=_a20_owned_identity(state).pid
    )
    return samples


def _preload_a20_qwen(state: A20RunState) -> None:
    preload = _a20_controller(state, action="preload_qwen")
    state.report["results"]["qwen_preload"] = safe_controller_result(preload)
    require(
        state.report,
        "qwen_preloaded_with_idle_runtime_guard",
        preload.get("ok") is True
        and QWEN_MODEL in _running_model_names(preload.get("running_after")),
        controller_code=preload.get("code"),
    )
    identity = _a20_owned_identity(state)
    require_only_test_owned_ollama(state.report, expected=identity, phase="after_preload")
    state.qwen_loaded_by_script = True
    state.cleanup["qwen_loaded_by_script"] = True
    state.report["results"]["resources_with_qwen"] = _resource_view(
        state.client, state.support, expected_ollama_pid=identity.pid
    )


def _exercise_a20_stt(state: A20RunState, samples: dict[str, Any]) -> None:
    client = state.client
    if client is None:
        raise ResourceEvidenceError("the isolated source sidecar client is unavailable")
    stt_settings = state.support.api_json(
        client,
        "PUT",
        "/api/stt/settings",
        json={
            "enabled": True,
            "provider": "faster_whisper",
            "model_id": STT_MODEL,
            "device": "cpu",
            "compute_type": "int8",
            "vad": True,
            "idle_unload_minutes": 0,
            "gpu_experimental": False,
        },
    )
    stt_load = state.support.api_json(
        client, "POST", "/api/stt/models/load", json={"model_id": STT_MODEL}
    )
    stt_status = state.support.api_json(client, "GET", "/api/stt/status")
    require(
        state.report,
        "cpu_int8_small_stt_resident_with_qwen",
        stt_settings.get("device") == "cpu"
        and stt_settings.get("compute_type") == "int8"
        and stt_load.get("status") == "READY"
        and stt_status.get("loaded_model") == STT_MODEL
        and isinstance(stt_status.get("worker_pid"), int),
        stt_settings=stt_settings,
        stt_load=stt_load,
        stt_status=stt_status,
    )
    transcription = state.support.run_transcription(
        client,
        conversation_id=state.support.create_conversation(client),
        sample=samples["chinese_5s"],
        model_id=STT_MODEL,
    )
    require(
        state.report,
        "real_cpu_small_stt_transcription_with_qwen",
        bool(str(transcription.get("text") or "").strip())
        and transcription.get("provider") == "faster_whisper"
        and transcription.get("model") == STT_MODEL,
        transcription={
            key: value for key, value in transcription.items() if key != "reference_text"
        },
    )
    state.report["results"]["stt_with_qwen"] = {
        "settings": stt_settings,
        "load": stt_load,
        "status": stt_status,
        "transcription": transcription,
        "resources": _resource_view(
            client, state.support, expected_ollama_pid=_a20_owned_identity(state).pid
        ),
    }


def _exercise_a20_tts(state: A20RunState) -> None:
    client = state.client
    if client is None:
        raise ResourceEvidenceError("the isolated source sidecar client is unavailable")
    tts = exercise_windows_tts_lifecycle(client, state.support)
    require(
        state.report,
        "real_windows_tts_with_qwen_and_stt",
        tts.get("provider") == "windows"
        and int(tts.get("duration_ms") or 0) > 0
        and int(tts.get("audio", {}).get("bytes") or 0) > 46
        and tts.get("playback", {}).get("completed_status") == "COMPLETED",
        tts=tts,
    )
    state.report["results"]["tts_with_qwen_and_stt"] = {
        **tts,
        "resources": _resource_view(
            client, state.support, expected_ollama_pid=_a20_owned_identity(state).pid
        ),
    }
    tts_residual = _safe_tts_temp_files(state.sidecar_runtime)
    state.report["results"]["tts_temp_residual_after_lifecycle"] = [
        path.name for path in tts_residual
    ]
    require(
        state.report,
        "test_owned_tts_lifecycle_converged",
        not tts_residual,
        residual=[path.name for path in tts_residual],
        request_id=tts.get("request_id"),
        task_id=tts.get("task_id"),
        playback=tts.get("playback"),
    )


def _run_a20_coexistence_sequence(state: A20RunState) -> None:
    _start_and_preflight_a20_ollama(state)
    samples = _prepare_a20_source_sidecar(state)
    _preload_a20_qwen(state)
    _exercise_a20_stt(state, samples)
    _exercise_a20_tts(state)
    require_only_test_owned_ollama(
        state.report, expected=_a20_owned_identity(state), phase="before_success"
    )


def _cleanup_a20_stt_and_qwen(state: A20RunState) -> None:
    client = state.client
    if client is not None:
        try:
            state.cleanup["stt_unload"] = state.support.api_json(
                client, "POST", "/api/stt/models/unload", json={"model_id": STT_MODEL}
            )
        except Exception as exc:
            state.cleanup["stt_unload_error"] = type(exc).__name__
            state.report["status"] = "FAIL"
    if state.qwen_loaded_by_script:
        try:
            observed = list_ollama_processes()
            state.cleanup["ollama_processes_before_unload"] = [
                identity_as_dict(item) for item in observed
            ]
            if state.test_owned_identity is None or observed != [state.test_owned_identity]:
                state.cleanup["qwen_unload_error"] = "TEST_OWNED_OLLAMA_PROCESS_SET_CHANGED"
                state.report["status"] = "FAIL"
            else:
                state.cleanup["qwen_unload"] = safe_controller_result(
                    _a20_controller(state, action="unload_owned_qwen")
                )
                after_names = _running_model_names(
                    state.cleanup["qwen_unload"].get("running_after")
                )
                if (
                    state.cleanup["qwen_unload"].get("ok") is not True
                    or QWEN_MODEL in after_names
                ):
                    state.cleanup["qwen_unload_error"] = str(
                        state.cleanup["qwen_unload"].get("code") or "qwen_remained_loaded"
                    )
                    state.report["status"] = "FAIL"
        except Exception as exc:
            state.cleanup["qwen_unload_error"] = type(exc).__name__
            state.report["status"] = "FAIL"
    if client is not None:
        try:
            expected_pid = state.test_owned_identity.pid if state.test_owned_identity else -1
            state.cleanup["resources_after_unload"] = _resource_view(
                client, state.support, expected_ollama_pid=expected_pid
            )
        except Exception as exc:
            state.cleanup["resources_after_unload_error"] = type(exc).__name__
            state.report["status"] = "FAIL"


def _cleanup_a20_sidecar_and_artifacts(state: A20RunState) -> None:
    if state.client is not None:
        state.client.close()
    try:
        state.cleanup["sidecar"] = state.support.stop_source_sidecar(
            state.source_process, state.stderr_handle, state.sidecar_identity
        )
        if state.cleanup["sidecar"].get("tree_cleanup", {}).get("tree_terminated") is not True:
            state.report["status"] = "FAIL"
    except Exception as exc:
        state.cleanup["sidecar_stop_error"] = type(exc).__name__
        state.report["status"] = "FAIL"
    try:
        residual = _safe_tts_temp_files(state.sidecar_runtime)
        state.cleanup["tts_temp_residual_after_sidecar_stop"] = [
            path.name for path in residual
        ]
        if residual:
            state.cleanup["tts_temp_residual_error"] = "TTS_TEMPORARY_AUDIO_RESIDUAL"
            state.report["status"] = "FAIL"
    except Exception as exc:
        state.cleanup["tts_temp_residual_check_error"] = type(exc).__name__
        state.report["status"] = "FAIL"
    try:
        state.cleanup["synthetic_corpus_cleanup"] = state.support.safe_remove_isolated_audio(
            state.sidecar_runtime
        )
    except Exception as exc:
        state.cleanup["synthetic_corpus_cleanup_error"] = type(exc).__name__
        state.report["status"] = "FAIL"
    if state.models_link is not None:
        try:
            state.support.remove_models_link(state.models_link, state.models_root)
            state.cleanup["formal_models_link_removed"] = True
        except Exception as exc:
            state.cleanup["formal_models_link_cleanup_error"] = type(exc).__name__
            state.report["status"] = "FAIL"


def _cleanup_a20_owned_ollama_and_store(state: A20RunState) -> None:
    if state.test_owned_service_start_attempted:
        try:
            state.cleanup["test_owned_ollama_stop"] = safe_controller_result(
                _a20_controller(state, action="stop_test_owned_service")
            )
            if not successful_test_owned_stop(state.cleanup["test_owned_ollama_stop"]):
                state.cleanup[
                    "test_owned_ollama_stop_error"
                ] = "TEST_OWNED_OLLAMA_STOP_CONTRACT_FAILED"
                state.report["status"] = "FAIL"
        except Exception as exc:
            state.cleanup["test_owned_ollama_stop_error"] = type(exc).__name__
            state.report["status"] = "FAIL"
    try:
        model_store_after = ollama_model_store_fingerprint(state.ollama_models_root)
        state.cleanup["ollama_model_store_after"] = model_store_after
        if model_store_after != state.report["results"].get("ollama_model_store_before"):
            state.cleanup["ollama_model_store_error"] = "MODEL_STORE_TREE_CHANGED"
            state.report["status"] = "FAIL"
    except Exception as exc:
        state.cleanup["ollama_model_store_error"] = type(exc).__name__
        state.report["status"] = "FAIL"
    try:
        final_ollama = list_ollama_processes()
        state.cleanup["ollama_processes_after_stop"] = [
            identity_as_dict(item) for item in final_ollama
        ]
        if final_ollama:
            state.cleanup["ollama_processes_after_stop_error"] = (
                "UNEXPECTED_OLLAMA_PROCESS_REMAINS"
            )
            state.report["status"] = "FAIL"
    except Exception as exc:
        state.cleanup["ollama_processes_after_stop_error"] = type(exc).__name__
        state.report["status"] = "FAIL"


def _settle_a20_run(state: A20RunState) -> None:
    _cleanup_a20_stt_and_qwen(state)
    _cleanup_a20_sidecar_and_artifacts(state)
    _cleanup_a20_owned_ollama_and_store(state)
    state.report["cleanup"] = state.cleanup
    state.report["finished_at"] = utc_now()


def _record_a20_failure(state: A20RunState, exc: Exception) -> None:
    state.report["actual_run"] = (
        state.test_owned_service_start_attempted or state.source_process is not None
    )
    state.report["status"] = "FAIL"
    state.report["failure"] = {
        "type": type(exc).__name__,
        "message": str(exc)
        .replace(str(state.runtime), "<isolated-runtime>")
        .replace(str(state.models_root), "<formal-model-root>")
        .replace(str(state.ollama_models_root), "<ollama-model-store>"),
    }


def run_live_resource_evidence(arguments: argparse.Namespace, output: Path) -> dict[str, Any]:
    """Run A20's safe current-source coexistence sequence once."""

    validate_selection(arguments)
    state = _new_a20_run_state(arguments, output)
    try:
        _run_a20_coexistence_sequence(state)
        state.report["actual_run"] = True
        state.report["status"] = "PASS"
        return state.report
    except Exception as exc:
        _record_a20_failure(state, exc)
        return state.report
    finally:
        _settle_a20_run(state)

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run explicit, safe A20 local qwen/STT/TTS resource-coexistence evidence."
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Repository-relative immutable JSON path under build/v1400-evidence.",
    )
    parser.add_argument(
        "--execute-live-resource-run",
        action="store_true",
        help="Required: authorizes the one real local qwen/STT/TTS coexistence sequence.",
    )
    parser.add_argument("--models-root", type=Path, default=default_models_root())
    parser.add_argument(
        "--ollama-models-root",
        type=Path,
        default=default_ollama_models_root(),
        help="Existing Ollama model store used with non-mutating API intent; the full regular-file tree is verified unchanged.",
    )
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--startup-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--controller-timeout-seconds", type=float, default=240.0)
    parser.add_argument(
        "--allow-test-owned-ollama-start",
        action="store_true",
        help="Required for A20: start only a 11435 test-owned service bound to the supplied opaque owner id.",
    )
    parser.add_argument("--test-owned-ollama-service-id")
    parser.add_argument(
        "--test-owned-ollama-executable",
        help="Optional explicit ollama.exe used only for an explicitly requested test-owned start.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        validate_selection(arguments)
        output = resolve_output_path(arguments.output)
    except ResourceEvidenceError as exc:
        print(f"v14 resource live evidence refused before execution: {exc}", file=sys.stderr)
        return 2
    if not arguments.execute_live_resource_run:
        print(
            "v14 resource live evidence refused: pass --execute-live-resource-run to authorize real local qwen/STT/TTS work",
            file=sys.stderr,
        )
        return 2
    if output.exists():
        print(f"v14 resource live evidence refused: refusing to overwrite {output.name}", file=sys.stderr)
        return 2
    report = run_live_resource_evidence(arguments, output)
    try:
        write_json_immutable(output, report)
    except ResourceEvidenceError as exc:
        print(f"v14 resource live evidence could not write output: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "actual_run": report["actual_run"],
                "output": output.relative_to(ROOT).as_posix(),
                "qwen_model": QWEN_MODEL,
                "model_download": "NOT_CALLED",
                "model_delete": "NOT_CALLED",
                "microphone_capture": "NOT_RUN",
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
