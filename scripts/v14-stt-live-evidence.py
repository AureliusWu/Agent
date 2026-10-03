from __future__ import annotations

"""Collect bounded, real local-STT evidence for the v14 acceptance ledger.

This is deliberately an operator-invoked repository script, not a test fixture.
It starts a *source* FastAPI sidecar on a random loopback port and normally
exercises the already-installed ``base`` Faster-Whisper model through the
public STT HTTP API.  One explicit operator-only CLI switch can instead prove
the formal ``small`` download-confirmation flow before exercising that model.

Safety properties are intentional and non-negotiable:

* its default path never calls a model download or delete endpoint;
* ``small`` download is possible only with ``--download-small-confirmed`` and
  is refused if any pre-existing ``small`` path would be overwritten;
* it never starts a microphone capture, records a microphone, or enumerates a
  device;
* it never calls an Ollama endpoint or changes an Ollama process;
* its database, logs, temporary audio, and sidecar state live in a new isolated
  runtime beneath the requested evidence output;
* the pre-existing production model root is linked into that isolated runtime
  rather than copied or modified; and
* corpus WAV files are generated from public synthetic text with Windows SAPI,
  explicitly marked as non-microphone audio, and removed before completion.

The script is useful for A08/A09/A10/A11/A20/A21 evidence, but it cannot prove
desktop microphone, renderer, or endurance acceptance.  Its JSON result says
that explicitly so a caller cannot promote a synthetic-file result into a
microphone PASS.

Typical invocation (normally wrapped by ``v14-evidence-runner.py``)::

    .\\siyi\\.venv\\Scripts\\python.exe scripts/v14-stt-live-evidence.py \
      --output build/v1400-evidence/raw/stt-live.json

The command exits non-zero whenever a required live API invariant fails.  It
always attempts to write the requested JSON evidence file after output-path
validation, including on a failed run.
"""

import argparse
import audioop
import difflib
import hashlib
import importlib.util
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import wave
from array import array
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
V14_EVIDENCE_ROOT = ROOT / "build" / "v1400-evidence"
EVIDENCE_RUNNER = ROOT / "scripts" / "v14-evidence-runner.py"
PYTHON = ROOT / "siyi" / ".venv" / "Scripts" / "python.exe"
SERVER = ROOT / "siyi" / "run_server.py"
MODEL_ID = "base"
MODEL_REPOSITORY = "Systran/faster-whisper-base"
DOWNLOAD_MODEL_ID = "small"
MODEL_REPOSITORIES = {
    MODEL_ID: MODEL_REPOSITORY,
    DOWNLOAD_MODEL_ID: "Systran/faster-whisper-small",
}
DOWNLOAD_TERMINAL_STATES = {"INSTALLED", "ERROR", "CANCELLED"}
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
MAX_CANCEL_MS = 500.0


class LiveEvidenceError(RuntimeError):
    """An observed live condition did not meet the bounded test contract."""


@dataclass(frozen=True)
class SourceSidecarIdentity:
    """A Windows-process identity captured for this script's isolated sidecar.

    A PID alone is intentionally not enough to authorize ``taskkill /T``:
    Windows can reuse a PID after the source sidecar exits.  The immutable
    process metadata below lets shutdown re-check that the PID still belongs to
    the exact Python ``run_server.py`` process this script launched before it
    ever asks Windows to terminate that process tree.
    """

    pid: int
    creation_date: str
    executable_path: str
    command_line: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def default_models_root() -> Path:
    local_app_data_text = os.environ.get("LOCALAPPDATA", "").strip()
    local_app_data = (
        Path(local_app_data_text).expanduser()
        if local_app_data_text
        else Path.home() / "AppData" / "Local"
    )
    return local_app_data / "AureliusWu" / "Agent" / "voice" / "models"


def display_path(path: Path) -> str:
    """Render a stable, non-user-specific local path for evidence."""

    resolved = path.resolve()
    local_app_data_text = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data_text:
        try:
            relative = resolved.relative_to(Path(local_app_data_text).resolve())
        except ValueError:
            pass
        else:
            return "%LOCALAPPDATA%/" + relative.as_posix()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return "<external-local-path>/" + resolved.name


def resolve_output_path(value: str, *, repository_root: Path = ROOT) -> Path:
    """Allow raw evidence only under the ignored v14 evidence root."""

    candidate = Path(value)
    root = repository_root.resolve()
    evidence_root = (root / "build" / "v1400-evidence").resolve()
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(evidence_root)
    except ValueError as exc:
        raise LiveEvidenceError("--output must stay under build/v1400-evidence") from exc
    if resolved.suffix.lower() != ".json":
        raise LiveEvidenceError("--output must end in .json")
    return resolved


def write_json_once(path: Path, payload: dict[str, Any]) -> None:
    """Persist UTF-8 evidence without overwriting an earlier immutable run."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileExistsError as exc:
        raise LiveEvidenceError(f"refusing to overwrite existing evidence: {path.name}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Preserve an interrupted evidence path rather than replacing it.
        raise


def model_metadata(
    models_root: Path,
    *,
    model_id: str = MODEL_ID,
    download_operation: str = "NOT_CALLED",
    user_confirmation: bool = False,
    installed_before_run: bool = True,
) -> dict[str, Any]:
    """Read only enough pre-existing model metadata to prove its local source."""

    root = models_root.expanduser().resolve()
    model = (root / model_id).resolve()
    if model.parent != root:
        raise LiveEvidenceError("model identifier escaped the configured models root")
    config = model / "config.json"
    if not config.is_file():
        raise LiveEvidenceError(
            "the requested local STT model is not installed; this script will not download it"
        )
    files = [item for item in model.rglob("*") if item.is_file()]
    return {
        "id": model_id,
        "repository": MODEL_REPOSITORIES.get(model_id, ""),
        "installed_before_run": installed_before_run,
        "installed_after_run": True,
        "model_root": display_path(root),
        "model_directory": display_path(model),
        "config_sha256": sha256(config),
        "file_count": len(files),
        "size_bytes": sum(item.stat().st_size for item in files),
        "download_operation": download_operation,
        "user_confirmation": user_confirmation,
        "delete_operation": "NOT_CALLED",
    }


def require_absent_download_target(models_root: Path, *, model_id: str = DOWNLOAD_MODEL_ID) -> Path:
    """Fail closed before a confirmed download could replace user-owned data."""

    root = models_root.expanduser().resolve()
    if not root.is_dir():
        raise LiveEvidenceError("configured formal models root does not exist")
    if model_id != DOWNLOAD_MODEL_ID:
        raise LiveEvidenceError("the live download path is restricted to the small model")
    target = root / model_id
    if target.parent.resolve() != root:
        raise LiveEvidenceError("download model identifier escaped the configured models root")
    # Path.exists() is false for a broken symlink.  lexists is intentional: an
    # unknown directory, file, junction, or broken link must all block a run
    # that could otherwise replace a pre-existing formal model path.
    if os.path.lexists(target):
        raise LiveEvidenceError("refusing confirmed small download because a small model path already exists")
    staging_names = sorted(
        item.name
        for item in root.iterdir()
        if item.name == f".{model_id}.downloading"
        or (
            item.name.startswith(f".{model_id}.")
            and item.name.endswith(".downloading")
        )
    )
    if staging_names:
        raise LiveEvidenceError(
            "refusing confirmed small download because a small staging path already exists"
        )
    return target


def model_file_manifest(models_root: Path, *, model_id: str) -> dict[str, Any]:
    """Hash every regular downloaded model file without following links."""

    root = models_root.expanduser().resolve()
    model = (root / model_id).resolve()
    if model.parent != root or not model.is_dir() or not (model / "config.json").is_file():
        raise LiveEvidenceError("downloaded STT model is not a complete managed model")
    files: list[dict[str, Any]] = []
    for item in sorted(model.rglob("*"), key=lambda value: value.as_posix().casefold()):
        if item.is_symlink():
            raise LiveEvidenceError("downloaded STT model contains an unsupported filesystem link")
        if not item.is_file():
            continue
        try:
            relative = item.resolve().relative_to(model)
        except ValueError as exc:
            raise LiveEvidenceError("downloaded STT model file escaped its managed directory") from exc
        files.append(
            {
                "path": relative.as_posix(),
                "size_bytes": item.stat().st_size,
                "sha256": sha256(item),
            }
        )
    if not files:
        raise LiveEvidenceError("downloaded STT model contains no files")
    return {
        "model": model_id,
        "model_directory": display_path(model),
        "file_count": len(files),
        "actual_bytes": sum(int(item["size_bytes"]) for item in files),
        "files": files,
    }


def _receipt_evidence_file(
    value: Path,
    *,
    repository_root: Path = ROOT,
) -> tuple[Path, str]:
    """Resolve an immutable prior-evidence input without leaving v14 evidence."""

    root = repository_root.resolve()
    evidence_root = (root / "build" / "v1400-evidence").resolve()
    candidate = value if value.is_absolute() else root / value
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(evidence_root)
    except (OSError, ValueError) as exc:
        raise LiveEvidenceError("download receipt inputs must stay under build/v1400-evidence") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise LiveEvidenceError("download receipt inputs must be regular JSON files")
    if resolved.suffix.lower() != ".json":
        raise LiveEvidenceError("download receipt inputs must be JSON files")
    return resolved, resolved.relative_to(root).as_posix()


def _read_receipt_json(path: Path, *, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        payload_bytes = path.read_bytes()
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveEvidenceError(f"{label} is not readable UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise LiveEvidenceError(f"{label} must contain a JSON object")
    return payload_bytes, payload


def _receipt_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validated_receipt_manifest(
    payload: object,
    *,
    expected_total: object,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LiveEvidenceError("prior download receipt is missing its final model manifest")
    files = payload.get("files")
    if (
        payload.get("model") != DOWNLOAD_MODEL_ID
        or not _receipt_positive_int(payload.get("file_count"))
        or not isinstance(files, list)
        or payload.get("file_count") != len(files)
        or payload.get("actual_bytes") != expected_total
    ):
        raise LiveEvidenceError("prior download receipt has an incomplete final model manifest")
    normalized_files: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise LiveEvidenceError("prior download receipt contains an invalid manifest entry")
        relative = item.get("path")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in seen
            or not _receipt_positive_int(size)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            raise LiveEvidenceError("prior download receipt contains an unsafe manifest entry")
        seen.add(relative)
        total += int(size)
        normalized_files.append(
            {"path": relative, "size_bytes": int(size), "sha256": digest.upper()}
        )
    if "config.json" not in seen or total != payload.get("actual_bytes"):
        raise LiveEvidenceError("prior download receipt manifest bytes do not balance")
    normalized_files.sort(key=lambda item: str(item["path"]).casefold())
    return {
        "model": DOWNLOAD_MODEL_ID,
        "file_count": len(normalized_files),
        "actual_bytes": total,
        "files": normalized_files,
    }


RECEIPT_SOURCE_FIELDS = (
    "source_version",
    "source_commit",
    "source_tree_fingerprint",
    "workspace_clean",
)


def _load_prior_download_receipt(
    prior_raw_value: Path,
    prior_envelope_value: Path,
    *,
    repository_root: Path,
) -> dict[str, Any]:
    prior_raw, raw_relative = _receipt_evidence_file(
        prior_raw_value, repository_root=repository_root
    )
    prior_envelope, envelope_relative = _receipt_evidence_file(
        prior_envelope_value, repository_root=repository_root
    )
    raw_bytes, raw = _read_receipt_json(prior_raw, label="prior download raw report")
    envelope_bytes, envelope = _read_receipt_json(
        prior_envelope, label="prior download runner envelope"
    )
    return {
        "raw_relative": raw_relative,
        "envelope_relative": envelope_relative,
        "raw_bytes": raw_bytes,
        "envelope_bytes": envelope_bytes,
        "raw": raw,
        "envelope": envelope,
    }


def _validate_prior_runner_envelope(receipt: dict[str, Any]) -> dict[str, Any]:
    raw = receipt["raw"]
    envelope = receipt["envelope"]
    execution = envelope.get("execution")
    contract = envelope.get("command_contract")
    command = envelope.get("command")
    if (
        envelope.get("schema_version") != 4
        or envelope.get("report_type") != "v14_execution"
        or envelope.get("producer") != "v14-evidence-runner"
        or envelope.get("target_version") != "14.0.0"
        or envelope.get("status") != "PASS"
        or envelope.get("actual_run") is not True
        or envelope.get("case_ids") != ["A09"]
        or not isinstance(execution, dict)
        or execution.get("actual_run") is not True
        or execution.get("status") != "PASS"
        or execution.get("exit_code") != 0
        or execution.get("timed_out") is not False
        or not isinstance(contract, dict)
        or contract.get("kind") != "repository_script"
        or contract.get("paths") != ["scripts/v14-stt-live-evidence.py"]
        or not isinstance(command, str)
        or "--download-small-confirmed" not in command.split()
        or "--download-only" not in command.split()
    ):
        raise LiveEvidenceError("prior download runner envelope is not a successful controlled A09 run")
    raw_source = raw.get("source")
    if (
        raw.get("report_type") != "v14_stt_live_evidence"
        or raw.get("producer") != "scripts/v14-stt-live-evidence.py"
        or raw.get("target_version") != "14.0.0"
        or raw.get("status") != "PASS"
        or raw.get("actual_run") is not True
        or not isinstance(raw_source, dict)
    ):
        raise LiveEvidenceError("prior download raw report is not a successful v14 STT run")
    if any(raw_source.get(field) != envelope.get(field) for field in RECEIPT_SOURCE_FIELDS):
        raise LiveEvidenceError("prior raw report source identity does not match its runner envelope")
    _validate_prior_raw_attachment(receipt, raw_source)
    return raw_source


def _validate_prior_raw_attachment(
    receipt: dict[str, Any],
    raw_source: dict[str, Any],
) -> None:
    raw = receipt["raw"]
    envelope = receipt["envelope"]
    attachments = envelope.get("attested_outputs")
    if not isinstance(attachments, list) or len(attachments) != 1 or not isinstance(attachments[0], dict):
        raise LiveEvidenceError("prior runner envelope must bind exactly one raw report")
    attachment = attachments[0]
    if (
        attachment.get("path") != receipt["raw_relative"]
        or attachment.get("sha256") != hashlib.sha256(receipt["raw_bytes"]).hexdigest().upper()
        or attachment.get("bytes") != len(receipt["raw_bytes"])
        or attachment.get("status") != raw.get("status")
        or attachment.get("actual_run") != raw.get("actual_run")
        or attachment.get("target_version") != raw.get("target_version")
        or attachment.get("report_type") != raw.get("report_type")
        or attachment.get("producer") != raw.get("producer")
        or attachment.get("source_identity_mode") != "raw_report"
        or any(attachment.get(field) != raw_source.get(field) for field in RECEIPT_SOURCE_FIELDS)
    ):
        raise LiveEvidenceError("prior runner envelope no longer binds the supplied raw report")


def _validate_prior_a09_scope(raw: dict[str, Any]) -> dict[str, Any]:
    required_checks = (
        "small_download_target_absent_before_run",
        "confirmed_small_download_via_formal_http_api",
        "cpu_int8_configuration",
        "isolated_sidecar_detects_formal_model",
        "real_cpu_int8_model_load",
        "real_explicit_model_unload",
        "stt_unload_memory_release_observation",
    )
    checks = raw.get("checks")
    if not isinstance(checks, dict) or any(
        not isinstance(checks.get(name), dict) or checks[name].get("passed") is not True
        for name in required_checks
    ):
        raise LiveEvidenceError("prior A09 raw report is missing a required passed check")
    scope, model, results = raw.get("scope"), raw.get("model"), raw.get("results")
    if (
        not isinstance(scope, dict)
        or scope.get("model_download") != "CALLED"
        or scope.get("user_confirmation") is not True
        or scope.get("download_only") is not True
        or scope.get("model_delete") != "NOT_CALLED"
        or not isinstance(model, dict)
        or model.get("id") != DOWNLOAD_MODEL_ID
        or model.get("installed_before_run") is not False
        or model.get("installed_after_run") is not True
        or model.get("download_operation") != "CALLED"
        or model.get("user_confirmation") is not True
        or model.get("delete_operation") != "NOT_CALLED"
        or not isinstance(results, dict)
    ):
        raise LiveEvidenceError("prior A09 raw report is not an actual confirmed download-only run")
    download = results.get("model_download")
    if not isinstance(download, dict):
        raise LiveEvidenceError("prior A09 raw report is missing model download details")
    return download


def _validate_prior_confirmation_flow(download: dict[str, Any]) -> dict[str, Any]:
    preview = download.get("preview")
    rejected = download.get("unconfirmed_request")
    confirmed = download.get("confirmed_request")
    final_state = download.get("final_state")
    if (
        not isinstance(preview, dict)
        or preview.get("model") != DOWNLOAD_MODEL_ID
        or preview.get("repo_id") != MODEL_REPOSITORIES[DOWNLOAD_MODEL_ID]
        or preview.get("already_installed") is not False
        or preview.get("fits") is not True
        or not _receipt_positive_int(preview.get("estimated_bytes"))
        or not _receipt_positive_int(preview.get("available_bytes"))
        or not _receipt_positive_int(preview.get("required_bytes"))
        or int(preview["available_bytes"]) < int(preview["required_bytes"])
        or not isinstance(rejected, dict)
        or rejected.get("confirmed") is not False
        or rejected.get("http_status") != 409
        or rejected.get("error_code") != "STT_DOWNLOAD_CONFIRMATION_REQUIRED"
        or rejected.get("download_started") is not False
        or not isinstance(confirmed, dict)
        or confirmed.get("confirmed") is not True
        or confirmed.get("called") is not True
        or confirmed.get("http_status") != 202
        or not isinstance(confirmed.get("response"), dict)
        or not isinstance(final_state, dict)
        or final_state.get("model") != DOWNLOAD_MODEL_ID
        or final_state.get("status") != "INSTALLED"
        or not _receipt_positive_int(final_state.get("completed_bytes"))
        or final_state.get("completed_bytes") != final_state.get("total_bytes")
        or final_state.get("error") is not None
    ):
        raise LiveEvidenceError("prior A09 raw report does not prove the formal confirmation flow")
    return final_state


def _normalize_current_small_manifest(models_root: Path) -> dict[str, Any]:
    current = model_file_manifest(models_root, model_id=DOWNLOAD_MODEL_ID)
    return {
        "model": current["model"],
        "file_count": current["file_count"],
        "actual_bytes": current["actual_bytes"],
        "files": [
            {
                "path": item["path"],
                "size_bytes": item["size_bytes"],
                "sha256": str(item["sha256"]).upper(),
            }
            for item in current["files"]
        ],
    }


def verify_prior_small_download_receipt(
    prior_raw_value: Path,
    prior_envelope_value: Path,
    models_root: Path,
    *,
    repository_root: Path = ROOT,
) -> dict[str, Any]:
    """Re-verify one actual small download without downloading or deleting."""

    receipt = _load_prior_download_receipt(
        prior_raw_value,
        prior_envelope_value,
        repository_root=repository_root,
    )
    raw_source = _validate_prior_runner_envelope(receipt)
    download = _validate_prior_a09_scope(receipt["raw"])
    final_state = _validate_prior_confirmation_flow(download)
    expected = _validated_receipt_manifest(
        download.get("final_model"), expected_total=final_state.get("completed_bytes")
    )
    current = _normalize_current_small_manifest(models_root)
    if current != expected:
        raise LiveEvidenceError("installed small model no longer matches the actual download receipt")
    return {
        "mode": "VERIFIED_PRIOR_ACTUAL",
        "prior_raw": {
            "path": receipt["raw_relative"],
            "sha256": hashlib.sha256(receipt["raw_bytes"]).hexdigest().upper(),
            "bytes": len(receipt["raw_bytes"]),
        },
        "prior_envelope": {
            "path": receipt["envelope_relative"],
            "sha256": hashlib.sha256(receipt["envelope_bytes"]).hexdigest().upper(),
            "bytes": len(receipt["envelope_bytes"]),
        },
        "prior_source": {field: raw_source[field] for field in RECEIPT_SOURCE_FIELDS},
        "runner_binding_verified": True,
        "formal_confirmation_flow_verified": True,
        "prior_user_confirmation_verified": True,
        "model_manifest_exact_match": True,
        "expected_model_manifest": expected,
        "current_model_manifest": current,
    }


def create_models_link(runtime: Path, models_root: Path) -> tuple[Path, str]:
    """Link the pre-existing production model root into a test-only runtime.

    The link itself is the only filesystem mutation outside the isolated DB/log
    paths.  It points *at* the formal model root and is removed later without
    recursing into its target.
    """

    target = models_root.resolve()
    if not target.is_dir():
        raise LiveEvidenceError("configured formal models root does not exist")
    link = runtime / "voice" / "models"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        raise LiveEvidenceError("isolated runtime already has a models directory")
    try:
        os.symlink(target, link, target_is_directory=True)
        link_kind = "directory_symlink"
    except OSError as symlink_error:
        # A directory junction is available on ordinary Windows installations
        # even where creating a symbolic link is not.  It remains a link in
        # the isolated runtime, never a copy of user-owned model files.
        environment = dict(os.environ)
        environment["SIYI_STT_ISOLATED_MODELS_LINK"] = str(link)
        environment["SIYI_STT_FORMAL_MODELS_ROOT"] = str(target)
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop';"
            "New-Item -ItemType Junction -Path $env:SIYI_STT_ISOLATED_MODELS_LINK "
            "-Target $env:SIYI_STT_FORMAL_MODELS_ROOT | Out-Null",
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise LiveEvidenceError(
                "could not link the formal model root into an isolated runtime; "
                "no model was copied, downloaded, or deleted"
            ) from symlink_error
        link_kind = "directory_junction"
    try:
        if link.resolve() != target:
            raise LiveEvidenceError("isolated models link does not resolve to the configured formal root")
    except Exception as exc:
        # ``link`` was created by this function just above.  Remove that one
        # link object if validation fails, but never recurse into its target.
        try:
            if link.exists() or link.is_symlink():
                os.rmdir(link)
        except OSError:
            pass
        if isinstance(exc, LiveEvidenceError):
            raise
        raise LiveEvidenceError("isolated models link could not be resolved") from exc
    return link, link_kind


def remove_models_link(link: Path, models_root: Path) -> None:
    """Remove only the known isolated link, never its user-owned target."""

    if not (link.exists() or link.is_symlink()):
        return
    expected = models_root.resolve()
    try:
        observed = link.resolve()
    except OSError as exc:
        raise LiveEvidenceError("refusing to remove an unreadable models link") from exc
    if observed != expected:
        raise LiveEvidenceError("refusing to remove a models link with an unexpected target")
    # os.rmdir removes a directory symlink/junction itself on Windows.  It
    # never recursively traverses the production model root.
    os.rmdir(link)


def safe_remove_isolated_audio(runtime: Path) -> list[str]:
    """Erase generated corpus and voice temp data from this one test runtime."""

    removed: list[str] = []
    for candidate in (runtime / "corpus", runtime / "voice" / "tmp"):
        try:
            candidate.resolve().relative_to(runtime.resolve())
        except ValueError as exc:
            raise LiveEvidenceError("isolated audio cleanup target escaped the test runtime") from exc
        if not candidate.exists():
            continue
        # These are test-owned, explicitly bounded folders.  Do not remove
        # runtime/voice itself because its sibling models link targets a formal
        # user-owned model directory.
        import shutil

        shutil.rmtree(candidate)
        removed.append(candidate.relative_to(runtime).as_posix())
    return removed


def _powershell_synthesize(text: str, output: Path, voice_name: str) -> None:
    """Create a public synthetic corpus source WAV without using a microphone."""

    script = (
        "Add-Type -AssemblyName System.Speech;"
        "[Console]::InputEncoding=[System.Text.Encoding]::UTF8;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$text=[Console]::In.ReadToEnd();"
        "$voice=$env:SIYI_STT_CORPUS_VOICE;if($voice){$s.SelectVoice($voice)};"
        "$s.SetOutputToWaveFile($env:SIYI_STT_CORPUS_OUTPUT);"
        "$s.Speak($text);$s.Dispose()"
    )
    environment = dict(os.environ)
    environment["SIYI_STT_CORPUS_VOICE"] = voice_name
    environment["SIYI_STT_CORPUS_OUTPUT"] = str(output)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        input=text.encode("utf-8"),
        capture_output=True,
        env=environment,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not output.is_file() or output.stat().st_size <= 46:
        raise LiveEvidenceError("Windows SAPI did not produce the synthetic Chinese corpus WAV")


def chinese_windows_voice() -> str:
    """Return one installed zh-CN SAPI voice, without playing audio."""

    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "@($s.GetInstalledVoices()|ForEach-Object{@{name=$_.VoiceInfo.Name;"
        "culture=$_.VoiceInfo.Culture.Name;enabled=$_.Enabled}})|ConvertTo-Json -Compress;"
        "$s.Dispose()"
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
    if result.returncode != 0 or not result.stdout.strip():
        raise LiveEvidenceError("Windows SAPI voice enumeration failed for the synthetic corpus")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LiveEvidenceError("Windows SAPI returned invalid voice metadata") from exc
    voices = parsed if isinstance(parsed, list) else [parsed]
    for item in voices:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False or str(item.get("culture") or "").lower() != "zh-cn":
            continue
        name = str(item.get("name") or "").strip()
        if name:
            return name
    raise LiveEvidenceError("no enabled zh-CN Windows SAPI voice is available for synthetic corpus generation")


def write_fixed_duration_wav(source: Path, output: Path, *, seconds: int) -> dict[str, int]:
    """Normalize and repeat public speech to an exact 16-kHz mono PCM length."""

    if seconds <= 0:
        raise ValueError("seconds must be positive")
    try:
        with wave.open(str(source), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            compression = handle.getcomptype()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        raise LiveEvidenceError("synthetic corpus source is not a readable WAV") from exc
    if compression != "NONE" or not frames:
        raise LiveEvidenceError("synthetic corpus source does not contain PCM speech frames")
    if channels not in {1, 2} or sample_width not in {1, 2, 3, 4} or sample_rate <= 0:
        raise LiveEvidenceError("synthetic corpus source has unsupported WAV parameters")
    if channels == 2:
        frames = audioop.tomono(frames, sample_width, 0.5, 0.5)
    if sample_width != SAMPLE_WIDTH:
        frames = audioop.lin2lin(frames, sample_width, SAMPLE_WIDTH)
    if sample_rate != SAMPLE_RATE:
        frames, _ = audioop.ratecv(frames, SAMPLE_WIDTH, CHANNELS, sample_rate, SAMPLE_RATE, None)
    target_bytes = seconds * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS
    if not frames:
        raise LiveEvidenceError("synthetic corpus conversion produced no speech frames")
    repeats = math.ceil(target_bytes / len(frames))
    normalized = (frames * repeats)[:target_bytes]
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(normalized)
    return {
        "duration_ms": seconds * 1000,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width": SAMPLE_WIDTH,
        "bytes": output.stat().st_size,
    }


def write_silence_wav(output: Path, *, seconds: int = 5) -> dict[str, int]:
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = b"\0" * (seconds * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(frames)
    return {
        "duration_ms": seconds * 1000,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width": SAMPLE_WIDTH,
        "bytes": output.stat().st_size,
    }


def write_deterministic_noise_overlay(source: Path, output: Path) -> dict[str, int]:
    """Add bounded, deterministic low-amplitude noise to one normalized PCM WAV.

    The generated noise is evidence data, not a microphone recording.  Keeping
    the seed and amplitude fixed makes the acceptance corpus reproducible while
    still exercising speech recognition away from a perfectly clean SAPI file.
    """

    try:
        with wave.open(str(source), "rb") as handle:
            if (
                handle.getcomptype() != "NONE"
                or handle.getnchannels() != CHANNELS
                or handle.getsampwidth() != SAMPLE_WIDTH
                or handle.getframerate() != SAMPLE_RATE
            ):
                raise LiveEvidenceError("noise source must be normalized 16-kHz mono PCM")
            frames = handle.readframes(handle.getnframes())
            frame_count = handle.getnframes()
    except (wave.Error, EOFError) as exc:
        raise LiveEvidenceError("noise source is not a readable normalized WAV") from exc
    if not frames or frame_count <= 0:
        raise LiveEvidenceError("noise source contains no PCM frames")

    state = 0x51A7
    noise = array("h")
    for _index in range(frame_count):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        # +/-192 is below 0.6% of full-scale 16-bit PCM.  It is audible enough
        # for a deterministic robustness probe without masking the SAPI voice.
        noise.append((state % 385) - 192)
    overlaid = audioop.add(frames, noise.tobytes(), SAMPLE_WIDTH)
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(overlaid)
    return {
        "duration_ms": round(frame_count / SAMPLE_RATE * 1000),
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width": SAMPLE_WIDTH,
        "bytes": output.stat().st_size,
        "deterministic_noise_peak": 192,
    }


def normalize_accuracy_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def character_accuracy_review(reference: str, observed: str) -> dict[str, Any]:
    """Return transparent machine assistance without deciding A08 PASS/FAIL."""

    expected = normalize_accuracy_text(reference)
    actual = normalize_accuracy_text(observed)
    previous = list(range(len(actual) + 1))
    for expected_character in expected:
        current = [previous[0] + 1]
        for index, actual_character in enumerate(actual, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[index] + 1,
                    previous[index - 1] + (expected_character != actual_character),
                )
            )
        previous = current
    errors = previous[-1]
    opcodes = []
    for tag, left_start, left_end, right_start, right_end in difflib.SequenceMatcher(
        a=expected,
        b=actual,
        autojunk=False,
    ).get_opcodes():
        if tag == "equal":
            continue
        opcodes.append(
            {
                "operation": tag,
                "reference": expected[left_start:left_end],
                "observed": actual[right_start:right_end],
            }
        )
    return {
        "reference_characters": len(expected),
        "observed_characters": len(actual),
        "edit_distance": errors,
        "cer": round(errors / max(1, len(expected)), 4),
        "character_differences": opcodes,
        "machine_assisted_only": True,
        "manual_accuracy_review_required": True,
    }


def build_synthetic_corpus(runtime: Path) -> tuple[dict[str, dict[str, Any]], str]:
    """Generate consent-safe public text corpus, never a private conversation."""

    corpus_root = runtime / "corpus"
    # Windows SAPI does not create a missing parent directory for its WAV
    # output.  Keep this test-owned directory creation explicit and reject a
    # collision rather than ever reusing an unknown corpus directory.
    corpus_root.mkdir(parents=True, exist_ok=False)
    raw = corpus_root / "source.wav"
    voice = chinese_windows_voice()
    chinese_reference = "司忆本地语音测试，今天是二零二六年八月九日，完成百分之九十五。"
    mixed_reference = "司忆 Agent 使用 Ollama 的 qwen 三 四 b 模型，并通过 FastAPI 提供本地服务。"
    proper_noun_reference = "夏目心正在检查项目报告，文件名是语音测试下划线最终版点 Markdown。"
    noise_pause_reference = "司忆，请暂停两秒，然后继续检查 API 日志和 README 文件。"
    _powershell_synthesize(chinese_reference, raw, voice)
    samples: dict[str, dict[str, Any]] = {}
    for name, seconds, reference, expected_entities in (
        ("chinese_5s", 5, chinese_reference, ["司忆"]),
        ("chinese_15s", 15, chinese_reference, ["司忆"]),
        ("chinese_30s", 30, chinese_reference, ["司忆"]),
    ):
        path = corpus_root / f"{name}.wav"
        properties = write_fixed_duration_wav(raw, path, seconds=seconds)
        samples[name] = {
            "path": path,
            "reference_text": reference,
            "expected_entities": expected_entities,
            "synthetic_non_microphone": True,
            "microphone_used": False,
            "acceptance_categories": ["ordinary_chinese", "numbers_dates_percent", "siyi"],
            **properties,
        }
    _powershell_synthesize(mixed_reference, raw, voice)
    mixed_path = corpus_root / "mixed_5s.wav"
    mixed_properties = write_fixed_duration_wav(raw, mixed_path, seconds=5)
    samples["mixed_5s"] = {
        "path": mixed_path,
        "reference_text": mixed_reference,
        "expected_entities": ["司忆", "Agent", "Ollama", "qwen", "FastAPI"],
        "synthetic_non_microphone": True,
        "microphone_used": False,
        "acceptance_categories": ["english_abbreviation", "mixed_language", "technical_terms", "siyi"],
        **mixed_properties,
    }
    _powershell_synthesize(proper_noun_reference, raw, voice)
    proper_path = corpus_root / "proper_nouns_8s.wav"
    proper_properties = write_fixed_duration_wav(raw, proper_path, seconds=8)
    samples["proper_nouns_8s"] = {
        "path": proper_path,
        "reference_text": proper_noun_reference,
        "expected_entities": ["夏目心", "Markdown"],
        "synthetic_non_microphone": True,
        "microphone_used": False,
        "acceptance_categories": ["natsume", "file_name", "technical_terms"],
        **proper_properties,
    }
    _powershell_synthesize(noise_pause_reference, raw, voice)
    clean_noise_source = corpus_root / "noise_pause_clean_8s.wav"
    write_fixed_duration_wav(raw, clean_noise_source, seconds=8)
    noise_path = corpus_root / "noise_pause_8s.wav"
    noise_properties = write_deterministic_noise_overlay(clean_noise_source, noise_path)
    clean_noise_source.unlink(missing_ok=True)
    samples["noise_pause_8s"] = {
        "path": noise_path,
        "reference_text": noise_pause_reference,
        "expected_entities": ["司忆", "API", "README"],
        "synthetic_non_microphone": True,
        "microphone_used": False,
        "acceptance_categories": ["pause", "background_noise", "english_abbreviation", "file_name"],
        **noise_properties,
    }
    silence_path = corpus_root / "blank_5s.wav"
    samples["blank_5s"] = {
        "path": silence_path,
        "reference_text": "",
        "expected_entities": [],
        "synthetic_non_microphone": True,
        "microphone_used": False,
        **write_silence_wav(silence_path),
    }
    raw.unlink(missing_ok=True)
    return samples, voice


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _normalise_windows_path(value: str | Path) -> str:
    return str(Path(value).resolve()).replace("/", "\\").casefold()


def _command_mentions_path(command_line: str, path: Path) -> bool:
    """Match an absolute script path without trusting a PID alone."""

    return _normalise_windows_path(path) in command_line.replace("/", "\\").casefold()


def _decode_windows_process_json(
    raw: bytes | str, *, ansi_encoding: str | None = "mbcs"
) -> dict[str, Any] | None:
    """Decode a PowerShell CIM JSON object without silently replacing bytes.

    Windows PowerShell 5.1 can emit redirected ``ConvertTo-Json`` output in
    the active ANSI code page even when this Python process is UTF-8.  Process
    identity is an authorization boundary, so ``errors='replace'`` is unsafe:
    it can turn a Chinese repository path into a different string and make an
    expected child look unrelated. Historical output may use the explicitly
    known ANSI code page; newly captured identities use UTF-8 only. Never guess
    a code page from the caller's language or accept replacement/best-fit bytes.
    """

    if isinstance(raw, str):
        candidates = (raw,)
    elif isinstance(raw, bytes):
        decoded: list[str] = []
        encodings = ("utf-8-sig", ansi_encoding)
        for encoding in dict.fromkeys(value.casefold() for value in encodings if value):
            try:
                candidate = raw.decode(encoding, errors="strict")
                original = raw.removeprefix(b"\xef\xbb\xbf") if encoding == "utf-8-sig" else raw
                output_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
                if candidate.encode(output_encoding, errors="strict") != original:
                    continue
                decoded.append(candidate)
            except (LookupError, UnicodeError):
                continue
        candidates = tuple(decoded)
    else:
        return None
    for candidate in candidates:
        if "\ufffd" in candidate:
            continue
        try:
            payload = json.loads(candidate)
            text = json.dumps(payload, ensure_ascii=False)
            text.encode("utf-8", errors="strict")
            if "\ufffd" in text:
                continue
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _read_windows_process_identity(pid: int) -> SourceSidecarIdentity | None:
    """Read stable identity metadata for one PID without changing process state."""

    if os.name != "nt" or pid <= 0:
        return None
    script = (
        f"$item=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' "
        "-ErrorAction SilentlyContinue;"
        "if($null -ne $item){"
        "$json=$item|Select-Object ProcessId,ExecutablePath,CommandLine,CreationDate|ConvertTo-Json -Compress;"
        "$bytes=[Text.Encoding]::UTF8.GetBytes($json);"
        "[Console]::OpenStandardOutput().Write($bytes,0,$bytes.Length)}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=False,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = _decode_windows_process_json(result.stdout, ansi_encoding=None)
        if payload is None:
            return None
        observed_pid = int(payload["ProcessId"])
        creation_date = str(payload["CreationDate"] or "").strip()
        executable_path = str(payload["ExecutablePath"] or "").strip()
        command_line = str(payload["CommandLine"] or "").strip()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if observed_pid != pid or not creation_date or not executable_path or not command_line:
        return None
    return SourceSidecarIdentity(
        pid=observed_pid,
        creation_date=creation_date,
        executable_path=executable_path,
        command_line=command_line,
    )


def _is_expected_source_sidecar(identity: SourceSidecarIdentity, *, expected_pid: int) -> bool:
    """Require the exact local Python launcher and local source entrypoint."""

    return (
        identity.pid == expected_pid
        and bool(identity.creation_date)
        and _normalise_windows_path(identity.executable_path) == _normalise_windows_path(PYTHON)
        and _command_mentions_path(identity.command_line, SERVER)
    )


def capture_source_sidecar_identity(
    process: subprocess.Popen[bytes],
    *,
    attempts: int = 20,
    retry_seconds: float = 0.05,
) -> SourceSidecarIdentity | None:
    """Capture a PID-reuse-safe identity before the sidecar can run STT work."""

    for attempt in range(max(1, attempts)):
        identity = _read_windows_process_identity(process.pid)
        if identity is not None and _is_expected_source_sidecar(identity, expected_pid=process.pid):
            return identity
        if process.poll() is not None:
            return None
        if attempt + 1 < max(1, attempts):
            time.sleep(retry_seconds)
    return None


def _run_taskkill_process_tree(pid: int) -> int | None:
    """Force-stop a confirmed Windows process tree; never select by image name."""

    if os.name != "nt" or pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    return result.returncode


def terminate_confirmed_source_sidecar_tree(
    process: subprocess.Popen[bytes] | None,
    identity: SourceSidecarIdentity | None,
) -> dict[str, Any]:
    """Terminate only this script's still-confirmed source sidecar tree.

    The recheck is deliberately performed immediately before ``taskkill``.  If
    the launcher exited, the PID was reused, WMI is unavailable, or the command
    no longer resolves to this repository's source sidecar, cleanup fails
    closed instead of risking an unrelated process (including external Ollama).
    """

    result: dict[str, Any] = {
        "attempted": False,
        "identity_confirmed": False,
        "tree_terminated": False,
    }
    if process is None:
        result.update({"not_required": True, "tree_terminated": True, "reason": "sidecar_not_started"})
        return result
    if process.poll() is not None:
        result["reason"] = "sidecar_exited_before_tree_cleanup"
        return result
    if identity is None:
        result["reason"] = "source_sidecar_identity_missing"
        return result

    observed = _read_windows_process_identity(process.pid)
    if (
        observed is None
        or observed != identity
        or not _is_expected_source_sidecar(observed, expected_pid=process.pid)
    ):
        result["reason"] = "source_sidecar_identity_mismatch"
        return result

    result["identity_confirmed"] = True
    result["attempted"] = True
    exit_code = _run_taskkill_process_tree(process.pid)
    result["taskkill_exit_code"] = exit_code
    if exit_code != 0:
        result["reason"] = "source_sidecar_tree_termination_failed"
        return result
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        result["reason"] = "source_sidecar_tree_termination_timeout"
        return result
    result["tree_terminated"] = process.poll() is not None
    if not result["tree_terminated"]:
        result["reason"] = "source_sidecar_tree_still_running"
    return result


def start_source_sidecar(
    runtime: Path,
    *,
    port: int,
    token: str,
) -> tuple[subprocess.Popen[bytes], Any, SourceSidecarIdentity]:
    if not PYTHON.is_file() or not SERVER.is_file():
        raise LiveEvidenceError("repository Python sidecar entrypoint is unavailable")
    runtime.mkdir(parents=True, exist_ok=True)
    stderr_path = runtime / "logs" / "sidecar.stderr.log"
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_handle = stderr_path.open("wb")
    environment = dict(os.environ)
    environment.update(
        {
            "AGENT_PORT": str(port),
            "AGENT_DATA_ROOT": str(runtime),
            "AGENT_DATABASE_PATH": str(runtime / "data" / "agent.db"),
            "AGENT_LOG_PATH": str(runtime / "logs" / "agent.log"),
            "AGENT_BIND_HOST": "127.0.0.1",
            "AGENT_API_TOKEN": token,
            "AGENT_DEPLOYMENT_MODE": "desktop_local",
            "AGENT_ALLOW_LOCAL_MCP": "false",
            "AGENT_ALLOW_PRIVATE_MODEL_PROVIDER": "false",
        }
    )
    process = subprocess.Popen(
        [str(PYTHON), str(SERVER)],
        cwd=SERVER.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr_handle,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    identity = capture_source_sidecar_identity(process)
    if identity is None:
        # This process object is held directly by the script and no API call
        # has run yet, so no Faster-Whisper worker could have been started.
        # Terminating this one launcher handle is safe; do not use PID-based
        # tree termination until the process identity can be verified.
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
        finally:
            stderr_handle.close()
        raise LiveEvidenceError("could not confirm the isolated source sidecar identity")
    return process, stderr_handle, identity


def stop_source_sidecar(
    process: subprocess.Popen[bytes] | None,
    stderr_handle: Any | None,
    identity: SourceSidecarIdentity | None,
) -> dict[str, Any]:
    """Stop only the confirmed source sidecar tree created by this script."""

    status: dict[str, Any] = {"started_pid": process.pid if process else None, "stopped": False}
    try:
        tree_cleanup = terminate_confirmed_source_sidecar_tree(process, identity)
        status["tree_cleanup"] = tree_cleanup
        if process is None:
            status.update({"stopped": True, "exit_code": None})
        else:
            status.update({"stopped": process.poll() is not None, "exit_code": process.returncode})
    finally:
        if stderr_handle is not None:
            stderr_handle.close()
    return status


def wait_ready(client: httpx.Client, process: subprocess.Popen[bytes], *, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LiveEvidenceError(f"source sidecar exited during startup with code {process.returncode}")
        try:
            response = client.get("/api/health", timeout=1.0)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    return payload
                last_error = "health payload was not ok"
            else:
                last_error = f"health status {response.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.1)
    raise LiveEvidenceError(f"source sidecar did not become healthy within {timeout_seconds:g}s ({last_error})")


def api_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected_status: int | tuple[int, ...] = 200,
    **kwargs: Any,
) -> dict[str, Any]:
    response = client.request(method, path, **kwargs)
    allowed = (expected_status,) if isinstance(expected_status, int) else expected_status
    if response.status_code not in allowed:
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("detail", body.get("status", "")))[:180]
        except ValueError:
            detail = response.text[:180]
        raise LiveEvidenceError(f"{method} {path} returned {response.status_code}, expected {allowed}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveEvidenceError(f"{method} {path} did not return JSON") from exc
    if not isinstance(payload, dict):
        raise LiveEvidenceError(f"{method} {path} returned a non-object JSON payload")
    return payload


def _response_json_object(response: httpx.Response, *, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveEvidenceError(f"{operation} did not return JSON") from exc
    if not isinstance(payload, dict):
        raise LiveEvidenceError(f"{operation} returned a non-object JSON payload")
    return payload


def _record_small_download_preview(
    client: httpx.Client,
    expected_target: Path,
    evidence: dict[str, Any],
) -> None:
    response = client.get(f"/api/stt/models/{DOWNLOAD_MODEL_ID}/download-preview")
    if response.status_code != 200:
        raise LiveEvidenceError(f"small download preview returned {response.status_code}")
    preview = _response_json_object(response, operation="small download preview")
    try:
        preview_target = Path(str(preview["target_directory"])).resolve()
        estimated_bytes = int(preview["estimated_bytes"])
        available_bytes = int(preview["available_bytes"])
        required_bytes = int(preview["required_bytes"])
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise LiveEvidenceError("small download preview was incomplete") from exc
    if (
        preview.get("model") != DOWNLOAD_MODEL_ID
        or preview.get("repo_id") != MODEL_REPOSITORIES[DOWNLOAD_MODEL_ID]
        or preview_target != expected_target
        or preview.get("already_installed") is not False
        or preview.get("fits") is not True
        or estimated_bytes <= 0
        or available_bytes < required_bytes
    ):
        raise LiveEvidenceError("small download preview did not authorize the expected managed target")
    evidence["preview"] = {
        "model": DOWNLOAD_MODEL_ID,
        "repo_id": preview.get("repo_id"),
        "estimated_bytes": estimated_bytes,
        "target_directory": display_path(expected_target),
        "already_installed": False,
        "available_bytes": available_bytes,
        "required_bytes": required_bytes,
        "fits": True,
    }


def _record_unconfirmed_download_rejection(
    client: httpx.Client,
    evidence: dict[str, Any],
) -> None:
    response = client.post(
        "/api/stt/models/download",
        json={"model_id": DOWNLOAD_MODEL_ID, "confirmed": False},
    )
    payload = _response_json_object(response, operation="unconfirmed small download request")
    detail = payload.get("detail")
    code = detail.get("code") if isinstance(detail, dict) else None
    evidence["unconfirmed_request"] = {
        "confirmed": False,
        "http_status": response.status_code,
        "error_code": code,
        "download_started": False,
    }
    if response.status_code != 409 or code != "STT_DOWNLOAD_CONFIRMATION_REQUIRED":
        raise LiveEvidenceError("formal STT API did not reject confirmed=false")


def _start_confirmed_small_download(
    client: httpx.Client,
    models_root: Path,
    evidence: dict[str, Any],
) -> None:
    # Re-check immediately before the only call that may write to the formal
    # model root. A concurrent path appearance must fail closed.
    require_absent_download_target(models_root)
    evidence["download_operation"] = "CALLED"
    evidence["confirmed_request"] = {"confirmed": True, "called": True}
    response = client.post(
        "/api/stt/models/download",
        json={"model_id": DOWNLOAD_MODEL_ID, "confirmed": True},
    )
    payload = _response_json_object(response, operation="confirmed small download request")
    evidence["confirmed_request"].update(
        {"http_status": response.status_code, "response": payload}
    )
    if response.status_code != 202:
        raise LiveEvidenceError(f"confirmed small download returned {response.status_code}")


def _wait_for_small_download(
    client: httpx.Client,
    evidence: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_sample: tuple[str, int, int] | None = None
    started = time.monotonic()
    while time.monotonic() < deadline:
        response = client.get(f"/api/stt/models/{DOWNLOAD_MODEL_ID}/download")
        if response.status_code != 200:
            raise LiveEvidenceError(f"small download status returned {response.status_code}")
        state = _response_json_object(response, operation="small download status")
        try:
            sample = (str(state["status"]), int(state["completed"]), int(state["total"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveEvidenceError("small download status was incomplete") from exc
        status_value, completed, total = sample
        if state.get("model") != DOWNLOAD_MODEL_ID or completed < 0 or total < 0:
            raise LiveEvidenceError("small download status identified an invalid operation")
        if sample != last_sample:
            evidence["progress"].append(
                {
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                    "status": status_value,
                    "completed_bytes": completed,
                    "total_bytes": total,
                }
            )
            last_sample = sample
        if status_value in DOWNLOAD_TERMINAL_STATES:
            return state
        time.sleep(0.35)
    raise LiveEvidenceError(f"small download did not finish within {timeout_seconds:g} seconds")


def download_small_model_via_api(
    client: httpx.Client,
    models_root: Path,
    *,
    user_confirmed: bool,
    timeout_seconds: float,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Exercise the formal small-model API flow after one explicit CLI grant."""

    evidence.update(
        {
            "model": DOWNLOAD_MODEL_ID,
            "user_confirmation": bool(user_confirmed),
            "download_operation": "NOT_CALLED",
            "delete_operation": "NOT_CALLED",
            "progress": [],
        }
    )
    if not user_confirmed:
        raise LiveEvidenceError("confirmed small download requires the explicit CLI authorization switch")
    target = require_absent_download_target(models_root)
    _record_small_download_preview(client, target, evidence)
    _record_unconfirmed_download_rejection(client, evidence)
    _start_confirmed_small_download(client, models_root, evidence)
    final_state = _wait_for_small_download(
        client, evidence, timeout_seconds=timeout_seconds
    )
    evidence["final_state"] = {
        "model": final_state.get("model"),
        "status": final_state.get("status"),
        "completed_bytes": final_state.get("completed"),
        "total_bytes": final_state.get("total"),
        "error": final_state.get("error"),
    }
    if final_state.get("status") != "INSTALLED":
        raise LiveEvidenceError(f"small download ended as {final_state.get('status')}")
    manifest = model_file_manifest(models_root, model_id=DOWNLOAD_MODEL_ID)
    evidence["final_model"] = manifest
    if int(final_state.get("completed") or 0) != int(manifest["actual_bytes"]):
        raise LiveEvidenceError("small download status bytes did not match the hashed final model")
    return evidence


def settle_small_download_before_shutdown(
    client: httpx.Client,
    models_root: Path,
    evidence: dict[str, Any],
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Cancel only this run's unfinished download before stopping the sidecar.

    A successful install is preserved.  The helper never removes either the
    final model or a staging directory; it asks the supervised manager to
    cancel, waits for a terminal state, and then verifies that no managed
    ``small`` staging directory remains.  Any uncertainty fails the evidence
    closed instead of force-deleting data.
    """

    result: dict[str, Any] = {
        "required": False,
        "cancel_called": False,
        "terminal_status": None,
        "owned_staging_paths_remaining": [],
        "settled": True,
    }
    if evidence.get("download_operation") != "CALLED":
        result["reason"] = "confirmed_download_not_started"
        return result

    final = evidence.get("final_state")
    final_status = str(final.get("status") or "") if isinstance(final, dict) else ""
    if final_status in DOWNLOAD_TERMINAL_STATES:
        result["terminal_status"] = final_status
    else:
        result["required"] = True
        response = client.post(
            "/api/stt/models/download/cancel",
            json={"model_id": DOWNLOAD_MODEL_ID},
        )
        if response.status_code == 200:
            result["cancel_called"] = True
        elif response.status_code == 409:
            payload = _response_json_object(response, operation="small download cancellation")
            detail = payload.get("detail")
            code = detail.get("code") if isinstance(detail, dict) else None
            if code != "STT_ALREADY_CANCELLED":
                raise LiveEvidenceError(
                    f"small download cancellation returned unexpected 409 code {code}"
                )
        else:
            raise LiveEvidenceError(
                f"small download cancellation returned {response.status_code}"
            )

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            state_response = client.get(f"/api/stt/models/{DOWNLOAD_MODEL_ID}/download")
            if state_response.status_code != 200:
                raise LiveEvidenceError(
                    f"small download settlement status returned {state_response.status_code}"
                )
            state = _response_json_object(
                state_response,
                operation="small download settlement status",
            )
            status_value = str(state.get("status") or "")
            if status_value in DOWNLOAD_TERMINAL_STATES:
                result["terminal_status"] = status_value
                break
            time.sleep(0.1)
        if result["terminal_status"] not in DOWNLOAD_TERMINAL_STATES:
            raise LiveEvidenceError("small download did not settle before sidecar shutdown")

    staging = sorted(
        item.name
        for item in models_root.iterdir()
        if item.name.startswith(f".{DOWNLOAD_MODEL_ID}.")
        and item.name.endswith(".downloading")
    )
    result["owned_staging_paths_remaining"] = staging
    result["settled"] = not staging
    if staging:
        raise LiveEvidenceError("small download staging directory remained after settlement")
    return result


def create_conversation(client: httpx.Client) -> int:
    payload = api_json(
        client,
        "POST",
        "/api/conversations",
        json={
            "title": "v14 synthetic local STT evidence",
            "workspace": "",
            "permission_mode": "ask",
            "agent_profile_id": "general",
        },
    )
    identifier = int(payload.get("id") or 0)
    if identifier <= 0:
        raise LiveEvidenceError("source sidecar did not create an isolated evidence conversation")
    return identifier


def create_voice_session(client: httpx.Client, conversation_id: int) -> str:
    payload = api_json(
        client,
        "POST",
        "/api/stt/sessions",
        expected_status=201,
        json={
            "conversation_id": conversation_id,
            # This is a marker only.  The script never asks the browser or OS
            # to open this named device.
            "device_id": "synthetic-non-microphone-corpus",
            "auto_send": False,
        },
    )
    value = str(payload.get("voice_session_id") or "")
    if len(value) < 16:
        raise LiveEvidenceError("STT session creation returned no usable session ID")
    return value


def run_transcription(
    client: httpx.Client,
    *,
    conversation_id: int,
    sample: dict[str, Any],
    model_id: str = MODEL_ID,
) -> dict[str, Any]:
    voice_session_id = create_voice_session(client, conversation_id)
    path = Path(sample["path"])
    started = time.perf_counter()
    with path.open("rb") as audio:
        response = client.post(
            "/api/stt/transcribe",
            data={"voice_session_id": voice_session_id},
            files={"file": (path.name, audio, "audio/wav")},
            timeout=180.0,
        )
    wall_ms = round((time.perf_counter() - started) * 1000.0, 3)
    if response.status_code != 200:
        raise LiveEvidenceError(f"local STT upload returned {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveEvidenceError("local STT upload did not return JSON") from exc
    if not isinstance(payload, dict):
        raise LiveEvidenceError("local STT upload returned a non-object JSON payload")
    transcription = payload.get("transcription")
    if not isinstance(transcription, dict):
        raise LiveEvidenceError("local STT upload did not return a transcription object")
    text = str(transcription.get("text") or "").strip()
    if not text:
        raise LiveEvidenceError("local STT returned an empty transcript for synthetic speech")
    if transcription.get("provider") != "faster_whisper" or transcription.get("model") != model_id:
        raise LiveEvidenceError("local STT response did not identify the configured Faster-Whisper model")
    transcript_summary = {
        "voice_session_id": voice_session_id,
        "audio_duration_ms": int(sample["duration_ms"]),
        "wall_ms": wall_ms,
        "provider": transcription.get("provider"),
        "model": transcription.get("model"),
        "language": transcription.get("language"),
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest().upper(),
        "segments": transcription.get("segments"),
        "transcription_ms": transcription.get("transcription_ms"),
        "real_time_factor": transcription.get("real_time_factor"),
        "reference_text": sample["reference_text"],
        "expected_entities": sample["expected_entities"],
        "synthetic_non_microphone": True,
        "manual_accuracy_review_required": True,
    }
    normalized = text.casefold()
    transcript_summary["observed_entities"] = [
        item for item in sample["expected_entities"] if item.casefold() in normalized
    ]
    transcript_summary["accuracy_review"] = character_accuracy_review(
        str(sample["reference_text"]),
        text,
    )
    # A successful transcription intentionally remains REVIEWING until the
    # human edits/sends/cancels it.  This file-only evidence never sends a
    # message to an Agent, so explicitly cancel the isolated review session
    # before starting the next independent sample and release its one-session
    # resource reservation.
    session_cleanup = api_json(
        client,
        "POST",
        f"/api/stt/sessions/{voice_session_id}/cancel",
    )
    if session_cleanup.get("state") != "CANCELLED":
        raise LiveEvidenceError("synthetic review session was not released after transcription")
    transcript_summary["session_cleanup_state"] = session_cleanup.get("state")
    return transcript_summary


def run_blank_rejection(client: httpx.Client, *, conversation_id: int, sample: dict[str, Any]) -> dict[str, Any]:
    voice_session_id = create_voice_session(client, conversation_id)
    path = Path(sample["path"])
    with path.open("rb") as audio:
        response = client.post(
            "/api/stt/transcribe",
            data={"voice_session_id": voice_session_id},
            files={"file": (path.name, audio, "audio/wav")},
            timeout=30.0,
        )
    detail: dict[str, Any] = {}
    try:
        parsed = response.json()
        detail = parsed.get("detail") if isinstance(parsed, dict) and isinstance(parsed.get("detail"), dict) else {}
    except ValueError:
        pass
    if response.status_code != 422 or detail.get("code") != "STT_NO_SPEECH":
        raise LiveEvidenceError("blank audio was not rejected as STT_NO_SPEECH")
    return {
        "voice_session_id": voice_session_id,
        "http_status": response.status_code,
        "error_code": detail.get("code"),
        "message_sent": False,
        "synthetic_non_microphone": True,
    }


def _upload_in_background(
    endpoint: str,
    headers: dict[str, str],
    voice_session_id: str,
    audio_path: Path,
    sink: dict[str, Any],
) -> None:
    try:
        with httpx.Client(base_url=endpoint, headers=headers, timeout=httpx.Timeout(180.0)) as client:
            with audio_path.open("rb") as audio:
                response = client.post(
                    "/api/stt/transcribe",
                    data={"voice_session_id": voice_session_id},
                    files={"file": (audio_path.name, audio, "audio/wav")},
                )
            try:
                payload: Any = response.json()
            except ValueError:
                payload = {"raw_response_length": len(response.content)}
            sink["response"] = {"status_code": response.status_code, "payload": payload}
    except BaseException as exc:  # Thread errors must be surfaced to the caller.
        sink["error"] = f"{type(exc).__name__}: {exc}"


def wait_for_active_transcription(
    client: httpx.Client,
    thread: threading.Thread,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_status = api_json(client, "GET", "/api/stt/status")
        if last_status.get("status") == "TRANSCRIBING" and last_status.get("active_requests"):
            return last_status
        if not thread.is_alive():
            break
        time.sleep(0.03)
    raise LiveEvidenceError(
        "the real STT request never became active, so cancellation was not falsely claimed"
    )


def active_request_id_for_cancellation(active: dict[str, Any]) -> str:
    """Return the one request the isolated evidence run is allowed to cancel.

    The live sidecar is test-owned, so a cancellation measurement with more
    than one active request is ambiguous rather than a reason to guess.  Using
    the exact ID observed immediately before the call also proves that a
    terminal ``CANCELLED`` response belongs to the request that was active,
    rather than treating a session-wide no-op as a pass.
    """

    active_requests = active.get("active_requests")
    if not isinstance(active_requests, list) or len(active_requests) != 1:
        raise LiveEvidenceError("real STT cancellation did not observe exactly one active request")
    raw_request_id = active_requests[0]
    request_id = raw_request_id.strip() if isinstance(raw_request_id, str) else ""
    if not request_id or len(request_id) > 128:
        raise LiveEvidenceError("real STT cancellation observed an invalid active request ID")
    return request_id


def targeted_cancellation_response(cancelled: dict[str, Any]) -> bool:
    """Accept only a targeted pending or fully settled cancellation.

    ``STTManager.cancel`` legitimately returns ``CANCELLED`` when task
    cancellation and worker teardown settle inside its bounded 500 ms wait.
    Requiring only ``CANCEL_REQUESTED`` would falsely reject that stronger,
    terminal result.  Zero-target and inconsistent settlement payloads remain
    hard failures.
    """

    status = str(cancelled.get("status") or "")
    cancelled_count = cancelled.get("cancelled")
    if not isinstance(cancelled_count, int) or cancelled_count < 1:
        return False
    if status == "CANCEL_REQUESTED":
        return cancelled.get("settled") is False
    return status == "CANCELLED" and cancelled.get("settled") is True


def run_cancellation(
    client: httpx.Client,
    *,
    endpoint: str,
    headers: dict[str, str],
    conversation_id: int,
    sample: dict[str, Any],
    cancel_threshold_ms: float,
) -> dict[str, Any]:
    """Cancel a real active multipart STT request, not a mocked worker."""

    voice_session_id = create_voice_session(client, conversation_id)
    result: dict[str, Any] = {}
    thread = threading.Thread(
        target=_upload_in_background,
        args=(endpoint, headers, voice_session_id, Path(sample["path"]), result),
        name="v14-stt-live-upload",
        daemon=True,
    )
    thread.start()
    active = wait_for_active_transcription(client, thread, timeout_seconds=20.0)
    request_id = active_request_id_for_cancellation(active)
    started = time.perf_counter()
    cancelled = api_json(
        client,
        "POST",
        "/api/stt/cancel",
        json={"request_id": request_id},
    )
    cancel_ms = round((time.perf_counter() - started) * 1000.0, 3)
    if not targeted_cancellation_response(cancelled):
        raise LiveEvidenceError("STT cancellation endpoint did not target the active real request")
    if cancel_ms > cancel_threshold_ms:
        raise LiveEvidenceError(
            f"STT cancellation exceeded the configured {cancel_threshold_ms:g} ms threshold ({cancel_ms:g} ms)"
        )
    thread.join(timeout=25.0)
    if thread.is_alive():
        raise LiveEvidenceError("STT upload remained active after cancellation")
    if "error" in result:
        raise LiveEvidenceError("STT upload thread failed while awaiting cancellation")
    response = result.get("response")
    if not isinstance(response, dict):
        raise LiveEvidenceError("STT upload returned no cancellation response")
    payload = response.get("payload")
    detail = payload.get("detail") if isinstance(payload, dict) and isinstance(payload.get("detail"), dict) else {}
    if int(response.get("status_code") or 0) != 409 or detail.get("code") != "STT_ALREADY_CANCELLED":
        raise LiveEvidenceError("cancelled real STT request did not return STT_ALREADY_CANCELLED")
    deadline = time.monotonic() + 6.0
    status = api_json(client, "GET", "/api/stt/status")
    while time.monotonic() < deadline and status.get("worker_pid") is not None:
        time.sleep(0.05)
        status = api_json(client, "GET", "/api/stt/status")
    if status.get("worker_pid") is not None:
        raise LiveEvidenceError("STT worker remained after real cancellation")
    return {
        "voice_session_id": voice_session_id,
        "request_id": request_id,
        "active_status_before_cancel": active.get("status"),
        "active_request_count": len(active.get("active_requests") or []),
        "cancel_response": cancelled,
        "cancel_ms": cancel_ms,
        "cancel_threshold_ms": cancel_threshold_ms,
        "upload_http_status": response.get("status_code"),
        "upload_error_code": detail.get("code"),
        "status_after_cancel": status,
        "synthetic_non_microphone": True,
    }


def _powershell_json(script: str) -> Any | None:
    """Read local OS counters without making an HTTP request to Ollama."""

    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return parsed


def _system_memory_snapshot() -> tuple[int | None, int | None]:
    payload = _powershell_json(
        "Get-CimInstance Win32_OperatingSystem | "
        "Select-Object TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json -Compress"
    )
    if not isinstance(payload, dict):
        return None, None
    try:
        return int(payload["TotalVisibleMemorySize"]) * 1024, int(payload["FreePhysicalMemory"]) * 1024
    except (KeyError, TypeError, ValueError):
        return None, None


def _process_rss_bytes(pid: int | None) -> int | None:
    if pid is None or pid <= 0:
        return None
    payload = _powershell_json(
        f"Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty WorkingSet64 | ConvertTo-Json -Compress"
    )
    return int(payload) if isinstance(payload, int) else None


def _gpu_memory_snapshot() -> tuple[int | None, int | None, bool]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None, None, False
    if result.returncode != 0:
        return None, None, False
    first = next((line for line in result.stdout.splitlines() if line.strip()), "")
    fields = [item.strip() for item in first.split(",")]
    if len(fields) != 2:
        return None, None, True
    try:
        return int(fields[0]) * 1024 * 1024, int(fields[1]) * 1024 * 1024, True
    except ValueError:
        return None, None, True


def resource_snapshot(client: httpx.Client) -> dict[str, Any]:
    """Capture STT and OS resource facts without consulting external Ollama.

    ``/api/local-models/resources`` intentionally reports Ollama state, so it
    is not safe for this no-Ollama live STT script.  The STT status route and
    read-only OS queries provide the exact worker PID and resource facts needed
    for this bounded test without touching port 11434.
    """

    status = api_json(client, "GET", "/api/stt/status")
    worker_pid = status.get("worker_pid")
    pid = int(worker_pid) if isinstance(worker_pid, int) and worker_pid > 0 else None
    total, available = _system_memory_snapshot()
    gpu_total, gpu_free, gpu_available = _gpu_memory_snapshot()
    return {
        "source": "STT loopback status plus read-only local OS counters; no Ollama endpoint invoked",
        "snapshot": {
            "system_total_bytes": total,
            "system_available_bytes": available,
            "gpu_total_bytes": gpu_total,
            "gpu_free_bytes": gpu_free,
            "gpu_observation_available": gpu_available,
            "stt_worker_pid": pid,
            "stt_rss_bytes": _process_rss_bytes(pid),
            "active_stt_requests": len(status.get("active_requests") or []),
            "loaded_model": status.get("loaded_model"),
            "status": status.get("status"),
        },
    }


def gpu_worker_observation(worker_pid: int | None) -> dict[str, Any]:
    """Read NVIDIA process data only; never reserve, unload, or start GPU work."""

    if not worker_pid or worker_pid <= 0:
        return {"available": False, "reason": "no_stt_worker_pid"}
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return {"available": False, "reason": "nvidia-smi-not-found"}
    if result.returncode != 0:
        return {"available": False, "reason": "nvidia-smi-query-failed"}
    entries: list[dict[str, int]] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        entries.append({"pid": int(parts[0]), "used_memory_mib": int(parts[1])})
    matched = next((item for item in entries if item["pid"] == worker_pid), None)
    return {
        "available": True,
        "stt_worker_pid": worker_pid,
        "worker_present_in_gpu_compute_processes": matched is not None,
        "worker_gpu_memory_mib": matched["used_memory_mib"] if matched else 0,
    }


def require(report: dict[str, Any], name: str, condition: bool, **details: Any) -> None:
    checks = report.setdefault("checks", {})
    checks[name] = {"passed": bool(condition), **details}
    if not condition:
        raise LiveEvidenceError(name)


def source_identity() -> dict[str, Any]:
    supplied = os.environ.get("SIYI_V14_EVIDENCE_SOURCE_IDENTITY")
    if supplied:
        try:
            identity = json.loads(supplied)
        except json.JSONDecodeError as exc:
            raise LiveEvidenceError("runner supplied an invalid source identity") from exc
        if not isinstance(identity, dict) or not isinstance(identity.get("workspace_clean"), bool):
            raise LiveEvidenceError("runner supplied an incomplete source identity")
        required = ("source_version", "source_commit", "source_tree_fingerprint")
        if not all(isinstance(identity.get(field), str) and identity[field] for field in required):
            raise LiveEvidenceError("runner supplied an incomplete source identity")
        return {field: identity[field] for field in (*required, "workspace_clean")}
    try:
        specification = importlib.util.spec_from_file_location("v14_evidence_runner_source_identity", EVIDENCE_RUNNER)
        if specification is None or specification.loader is None:
            raise LiveEvidenceError("could not load the v14 evidence runner identity helper")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        identity = module.source_identity(ROOT)
    except (OSError, ValueError) as exc:
        raise LiveEvidenceError("could not calculate the current source identity") from exc
    return dict(identity)


@dataclass
class SttLiveRunState:
    arguments: argparse.Namespace
    models_root: Path
    runtime: Path
    model_id: str
    download_small_confirmed: bool
    receipt_verification: bool
    download_only: bool
    receipt_raw: Path | None
    receipt_envelope: Path | None
    report: dict[str, Any]
    model_link: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    stderr_handle: Any | None = None
    sidecar_identity: SourceSidecarIdentity | None = None
    client: httpx.Client | None = None
    download_evidence: dict[str, Any] | None = None
    samples: dict[str, dict[str, Any]] = field(default_factory=dict)
    endpoint: str = ""
    token: str = ""
    cleanup: dict[str, Any] = field(
        default_factory=lambda: {
            "generated_audio_removed": [],
            "models_link_removed": False,
        }
    )


def _create_stt_live_run(
    arguments: argparse.Namespace,
    output: Path,
) -> SttLiveRunState:
    models_root = arguments.models_root.expanduser().resolve()
    download_confirmed = bool(getattr(arguments, "download_small_confirmed", False))
    receipt_raw = getattr(arguments, "verify_download_receipt", None)
    receipt_envelope = getattr(arguments, "receipt_envelope", None)
    receipt_verification = receipt_raw is not None
    download_only = bool(getattr(arguments, "download_only", False)) or receipt_verification
    requested_model = str(getattr(arguments, "model_id", MODEL_ID))
    model_id = DOWNLOAD_MODEL_ID if (download_confirmed or receipt_verification) else requested_model
    run_id = uuid.uuid4().hex
    runtime = output.parent / f"{output.stem}-runtime-{run_id[:12]}"
    model_scope = f"pre-existing formally stored Faster-Whisper {model_id} model"
    if download_confirmed:
        model_scope = "explicitly confirmed Faster-Whisper small model download through the formal STT HTTP API"
    elif receipt_verification:
        model_scope = "current formal Faster-Whisper small model verified against a prior actual-download receipt"
    report: dict[str, Any] = {
        "schema_version": 1,
        "report_type": "v14_stt_live_evidence",
        "producer": "scripts/v14-stt-live-evidence.py",
        "target_version": "14.0.0",
        "recorded_at": utc_now(),
        "status": "FAIL",
        "actual_run": False,
        "source": source_identity(),
        "scope": {
            "sidecar": "source FastAPI sidecar on a random 127.0.0.1 port",
            "model": model_scope,
            "corpus": "synthetic non-microphone Windows SAPI WAVs with public fixed text",
            "microphone_capture": "NOT_RUN",
            "renderer_desktop_flow": "NOT_RUN",
            "endurance": "NOT_RUN",
            "ollama_actions": "NONE",
            "model_download": "VERIFIED_PRIOR_ACTUAL" if receipt_verification else "NOT_CALLED",
            "user_confirmation": download_confirmed,
            "prior_user_confirmation_verified": receipt_verification,
            "model_delete": "NOT_CALLED",
            "download_only": download_only,
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
            "合成音频测试不代表麦克风真实录音通过。",
            "文件上传转写不代表桌面前端联通、编辑或消息发送通过。",
            "固定合成语料保留原文与转写供人工准确性核对，不把机器字符串比较伪装成真实录音准确率。",
            "本脚本未调用 Ollama，不能替代 Ollama 生命周期验收。",
        ],
    }
    return SttLiveRunState(
        arguments=arguments,
        models_root=models_root,
        runtime=runtime,
        model_id=model_id,
        download_small_confirmed=download_confirmed,
        receipt_verification=receipt_verification,
        download_only=download_only,
        receipt_raw=receipt_raw,
        receipt_envelope=receipt_envelope,
        report=report,
    )


def _prepare_stt_model_scope(state: SttLiveRunState) -> None:
    report = state.report
    if state.download_small_confirmed:
        require_absent_download_target(state.models_root)
        report["checks"]["small_download_target_absent_before_run"] = {
            "passed": True,
            "model": DOWNLOAD_MODEL_ID,
            "overwrite_permitted": False,
        }
    elif state.receipt_verification:
        if not isinstance(state.receipt_raw, Path) or not isinstance(state.receipt_envelope, Path):
            raise LiveEvidenceError("receipt verification requires both prior raw and runner envelope paths")
        receipt = verify_prior_small_download_receipt(
            state.receipt_raw,
            state.receipt_envelope,
            state.models_root,
        )
        report["results"]["download_receipt_verification"] = receipt
        report["checks"]["small_download_target_absent_before_run"] = {
            "passed": True,
            "observation_mode": "VERIFIED_PRIOR_ACTUAL",
            "prior_actual_download_receipt": True,
        }
        report["checks"]["confirmed_small_download_via_formal_http_api"] = {
            "passed": True,
            "observation_mode": "VERIFIED_PRIOR_ACTUAL",
            "prior_runner_binding_verified": True,
            "prior_user_confirmation_verified": True,
        }
        report["checks"]["current_small_manifest_matches_download_receipt"] = {
            "passed": receipt.get("model_manifest_exact_match") is True,
            "file_count": receipt.get("current_model_manifest", {}).get("file_count"),
            "actual_bytes": receipt.get("current_model_manifest", {}).get("actual_bytes"),
        }
        report["model"] = model_metadata(
            state.models_root,
            model_id=DOWNLOAD_MODEL_ID,
            download_operation="VERIFIED_PRIOR_ACTUAL",
            user_confirmation=False,
            installed_before_run=True,
        )
        report["model"]["prior_user_confirmation_verified"] = True
    else:
        report["model"] = model_metadata(state.models_root, model_id=state.model_id)
        require(report, "preexisting_formal_model", True, **report["model"])


def _prepare_stt_runtime_files(state: SttLiveRunState) -> None:
    state.model_link, link_kind = create_models_link(state.runtime, state.models_root)
    state.report["runtime"]["formal_model_link"] = {
        "kind": link_kind,
        "target": display_path(state.models_root),
        "copy_performed": False,
    }
    if state.download_only:
        reason = "A09 download-only evidence does not run transcription corpus"
        if state.receipt_verification:
            reason = "A09 receipt re-verification does not run transcription corpus"
        state.report["corpus"] = {
            "kind": "NOT_RUN",
            "reason": reason,
            "microphone_used": False,
        }
        return
    state.samples, corpus_voice = build_synthetic_corpus(state.runtime)
    state.report["corpus"] = {
        "kind": "synthetic_non_microphone",
        "generator": "Windows SAPI",
        "voice": corpus_voice,
        "microphone_used": False,
        "samples": {
            name: {key: value for key, value in sample.items() if key != "path"}
            for name, sample in state.samples.items()
        },
    }


def _start_stt_live_sidecar(state: SttLiveRunState) -> None:
    port = free_loopback_port()
    state.token = uuid.uuid4().hex
    state.endpoint = f"http://127.0.0.1:{port}"
    state.process, state.stderr_handle, state.sidecar_identity = start_source_sidecar(
        state.runtime,
        port=port,
        token=state.token,
    )
    state.client = httpx.Client(
        base_url=state.endpoint,
        headers={"X-Agent-Api-Token": state.token},
        timeout=httpx.Timeout(30.0),
    )
    health = wait_ready(
        state.client,
        state.process,
        timeout_seconds=state.arguments.startup_timeout_seconds,
    )
    state.report["results"]["sidecar"] = {
        "loopback_endpoint": "http://127.0.0.1:<ephemeral>",
        "health_status": health.get("status"),
        "version": health.get("version"),
        "pid": state.process.pid,
        "identity_captured": True,
    }
    require(state.report, "source_sidecar_loopback_ready", health.get("status") == "ok")


def _download_small_for_stt_run(state: SttLiveRunState) -> None:
    if not state.download_small_confirmed:
        return
    assert state.client is not None
    state.download_evidence = {}
    state.report["results"]["model_download"] = state.download_evidence
    try:
        download_small_model_via_api(
            state.client,
            state.models_root,
            user_confirmed=True,
            timeout_seconds=state.arguments.download_timeout_seconds,
            evidence=state.download_evidence,
        )
    finally:
        state.report["scope"]["model_download"] = state.download_evidence.get(
            "download_operation", "NOT_CALLED"
        )
    state.report["model"] = model_metadata(
        state.models_root,
        model_id=state.model_id,
        download_operation="CALLED",
        user_confirmation=True,
        installed_before_run=False,
    )
    evidence = state.download_evidence
    require(
        state.report,
        "confirmed_small_download_via_formal_http_api",
        evidence.get("download_operation") == "CALLED"
        and evidence.get("user_confirmation") is True
        and evidence.get("unconfirmed_request", {}).get("error_code")
        == "STT_DOWNLOAD_CONFIRMATION_REQUIRED"
        and evidence.get("final_state", {}).get("status") == "INSTALLED",
        preview_estimated_bytes=evidence.get("preview", {}).get("estimated_bytes"),
        final_actual_bytes=evidence.get("final_model", {}).get("actual_bytes"),
    )


def _configure_stt_cpu_model(state: SttLiveRunState) -> None:
    assert state.client is not None
    settings = api_json(
        state.client,
        "PUT",
        "/api/stt/settings",
        json={
            "enabled": True,
            "provider": "faster_whisper",
            "model_id": state.model_id,
            "device": "cpu",
            "compute_type": "int8",
            "vad": True,
            "idle_unload_minutes": 0,
            "gpu_experimental": False,
        },
    )
    state.report["results"]["settings"] = settings
    require(
        state.report,
        "cpu_int8_configuration",
        settings.get("device") == "cpu"
        and settings.get("compute_type") == "int8"
        and settings.get("gpu_experimental") is False,
    )


def _load_stt_cpu_model(state: SttLiveRunState) -> dict[str, Any]:
    assert state.client is not None
    response = state.client.get("/api/stt/models")
    if response.status_code != 200:
        raise LiveEvidenceError("STT models endpoint did not return 200")
    payload = response.json()
    if not isinstance(payload, list):
        raise LiveEvidenceError("STT models endpoint did not return a list")
    model_state = next(
        (
            item
            for item in payload
            if isinstance(item, dict) and item.get("id") == state.model_id
        ),
        None,
    )
    if not isinstance(model_state, dict):
        raise LiveEvidenceError(f"STT models endpoint did not report {state.model_id}")
    state.report["results"]["model_list_before_load"] = {
        key: model_state.get(key)
        for key in (
            "id",
            "provider",
            "repo_id",
            "installed",
            "status",
            "size_bytes",
            "loaded",
            "device",
            "compute_type",
        )
    }
    require(state.report, "isolated_sidecar_detects_formal_model", bool(model_state.get("installed")))
    before = resource_snapshot(state.client)
    started = time.perf_counter()
    loaded = api_json(
        state.client,
        "POST",
        "/api/stt/models/load",
        json={"model_id": state.model_id},
    )
    wall_ms = round((time.perf_counter() - started) * 1000.0, 3)
    status = api_json(state.client, "GET", "/api/stt/status")
    state.report["results"]["load"] = {
        "response": loaded,
        "wall_ms": wall_ms,
        "status": status,
        "resources_before": before,
        "resources_loaded": resource_snapshot(state.client),
    }
    worker_pid = status.get("worker_pid")
    require(
        state.report,
        "real_cpu_int8_model_load",
        loaded.get("status") == "READY"
        and loaded.get("device") == "cpu"
        and loaded.get("compute_type") == "int8"
        and status.get("loaded_model") == state.model_id
        and isinstance(worker_pid, int)
        and worker_pid > 0,
    )
    _record_stt_gpu_observation(state, worker_pid)
    return status


def _record_stt_gpu_observation(
    state: SttLiveRunState,
    worker_pid: object,
) -> None:
    gpu = gpu_worker_observation(worker_pid if isinstance(worker_pid, int) else None)
    state.report["results"]["gpu_observation"] = gpu
    if gpu.get("available"):
        require(
            state.report,
            "cpu_stt_worker_absent_from_gpu_compute_processes",
            gpu.get("worker_present_in_gpu_compute_processes") is False,
            **gpu,
        )
        return
    state.report["checks"]["cpu_stt_worker_absent_from_gpu_compute_processes"] = {
        "passed": None,
        "reason": str(gpu.get("reason") or "GPU observation unavailable"),
    }
    if state.arguments.require_gpu_observation:
        raise LiveEvidenceError("GPU observation was required but unavailable")


def _unload_stt_model(
    state: SttLiveRunState,
    *,
    status_before: dict[str, Any],
    reload_response: dict[str, Any] | None = None,
) -> bool:
    assert state.client is not None
    resources_before = resource_snapshot(state.client)
    unloaded = api_json(
        state.client,
        "POST",
        "/api/stt/models/unload",
        json={"model_id": state.model_id},
    )
    deadline = time.monotonic() + 6.0
    status_after = api_json(state.client, "GET", "/api/stt/status")
    while time.monotonic() < deadline and status_after.get("worker_pid") is not None:
        time.sleep(0.05)
        status_after = api_json(state.client, "GET", "/api/stt/status")
    resources_after = resource_snapshot(state.client)
    rss_before = resources_before["snapshot"].get("stt_rss_bytes")
    rss_after = resources_after["snapshot"].get("stt_rss_bytes")
    observed_release = (
        isinstance(rss_before, int)
        and rss_before > 0
        and (rss_after is None or (isinstance(rss_after, int) and rss_after < rss_before))
    )
    result = {
        "status_before": status_before,
        "resources_before": resources_before,
        "unload_response": unloaded,
        "status_after": status_after,
        "resources_after": resources_after,
        "resource_release": {
            "worker_gone": status_after.get("worker_pid") is None,
            "stt_rss_before_bytes": rss_before,
            "stt_rss_after_bytes": rss_after,
            "observed_release": observed_release,
        },
    }
    if reload_response is not None:
        result = {"reload_response": reload_response, **result}
    state.report["results"]["unload"] = result
    ready = reload_response is None or reload_response.get("status") == "READY"
    require(
        state.report,
        "real_explicit_model_unload",
        ready
        and unloaded.get("status") == "UNLOADED"
        and status_after.get("loaded_model") is None
        and status_after.get("worker_pid") is None,
    )
    return observed_release


ACCEPTANCE_SAMPLE_NAMES = (
    "chinese_5s",
    "chinese_15s",
    "chinese_30s",
    "mixed_5s",
    "proper_nouns_8s",
    "noise_pause_8s",
)

REQUIRED_ACCURACY_CATEGORIES = {
    "ordinary_chinese",
    "numbers_dates_percent",
    "english_abbreviation",
    "mixed_language",
    "siyi",
    "natsume",
    "file_name",
    "technical_terms",
    "pause",
    "background_noise",
}


def _run_stt_acceptance_transcriptions(
    state: SttLiveRunState,
    conversation_id: str,
) -> dict[str, Any]:
    assert state.client is not None
    results = {
        name: run_transcription(
            state.client,
            conversation_id=conversation_id,
            sample=state.samples[name],
            model_id=state.model_id,
        )
        for name in ACCEPTANCE_SAMPLE_NAMES
    }
    state.report["results"]["transcriptions"] = results
    categories = sorted(
        {
            category
            for name in ACCEPTANCE_SAMPLE_NAMES
            for category in state.samples[name].get("acceptance_categories", [])
        }
    )
    state.report["results"]["accuracy_review"] = {
        "manual_review_required": True,
        "decision": "NOT_AUTOMATED",
        "categories_exercised": categories,
        "samples": {
            name: {
                "reference_text": results[name]["reference_text"],
                "observed_text": results[name]["text"],
                "expected_entities": results[name]["expected_entities"],
                "observed_entities": results[name]["observed_entities"],
                "accuracy_review": results[name]["accuracy_review"],
            }
            for name in ACCEPTANCE_SAMPLE_NAMES
        },
        "note": "CER and character differences assist review; they do not decide A08 without human inspection.",
    }
    require(
        state.report,
        "fixed_accuracy_corpus_category_coverage",
        REQUIRED_ACCURACY_CATEGORIES.issubset(categories),
        categories_exercised=categories,
        manual_accuracy_review_required=True,
    )
    five_second = results["chinese_5s"]
    fifteen_second = results["chinese_15s"]
    require(
        state.report,
        "five_second_hot_transcription_budget",
        isinstance(five_second.get("wall_ms"), (int, float))
        and float(five_second["wall_ms"]) <= 5000.0,
        observed_ms=five_second.get("wall_ms"),
        threshold_ms=5000.0,
    )
    require(
        state.report,
        "fifteen_second_real_time_factor_budget",
        isinstance(fifteen_second.get("real_time_factor"), (int, float))
        and float(fifteen_second["real_time_factor"]) <= 1.5,
        observed_rtf=fifteen_second.get("real_time_factor"),
        threshold_rtf=1.5,
    )
    require(
        state.report,
        "synthetic_chinese_and_mixed_transcripts_nonempty",
        all(str(value.get("text") or "").strip() for value in results.values()),
        manual_accuracy_review_required=True,
    )
    return results


def _run_stt_rejection_cancellation_and_unload(
    state: SttLiveRunState,
    conversation_id: str,
) -> None:
    assert state.client is not None
    state.report["results"]["blank_rejection"] = run_blank_rejection(
        state.client,
        conversation_id=conversation_id,
        sample=state.samples["blank_5s"],
    )
    require(state.report, "blank_audio_rejected_without_message", True)
    state.report["results"]["cancellation"] = run_cancellation(
        state.client,
        endpoint=state.endpoint,
        headers={"X-Agent-Api-Token": state.token},
        conversation_id=conversation_id,
        sample=state.samples["chinese_30s"],
        cancel_threshold_ms=state.arguments.cancel_threshold_ms,
    )
    require(state.report, "real_stt_cancellation", True)
    # Cancellation terminates the supervised worker. Reload once so explicit
    # unload observes a live native child instead of inferring cleanup.
    reloaded = api_json(
        state.client,
        "POST",
        "/api/stt/models/load",
        json={"model_id": state.model_id},
    )
    status_before = api_json(state.client, "GET", "/api/stt/status")
    released = _unload_stt_model(
        state,
        status_before=status_before,
        reload_response=reloaded,
    )
    state.report["checks"]["stt_unload_memory_release_observation"] = {
        "passed": released,
        "required_for_resource_pass": True,
        "note": "Dedicated STT child RSS is sampled before unload and worker absence after unload is also recorded.",
    }
    state.report["results"]["metrics"] = api_json(
        state.client,
        "GET",
        "/api/stt/metrics",
    )


def _execute_stt_live_run(state: SttLiveRunState) -> None:
    _prepare_stt_model_scope(state)
    _prepare_stt_runtime_files(state)
    _start_stt_live_sidecar(state)
    _download_small_for_stt_run(state)
    _configure_stt_cpu_model(state)
    status_loaded = _load_stt_cpu_model(state)
    if state.download_only:
        released = _unload_stt_model(state, status_before=status_loaded)
        require(
            state.report,
            "stt_unload_memory_release_observation",
            released,
            required_for_resource_pass=True,
        )
        return
    assert state.client is not None
    conversation_id = create_conversation(state.client)
    _run_stt_acceptance_transcriptions(state, conversation_id)
    _run_stt_rejection_cancellation_and_unload(state, conversation_id)


def _cleanup_stt_live_run(state: SttLiveRunState) -> None:
    if state.client is not None and state.download_evidence is not None:
        try:
            state.cleanup["download_settlement"] = settle_small_download_before_shutdown(
                state.client,
                state.models_root,
                state.download_evidence,
            )
        except Exception as exc:
            state.cleanup["download_settlement"] = {
                "settled": False,
                "error": type(exc).__name__,
            }
            state.report["status"] = "FAIL"
    if state.client is not None:
        state.client.close()
    try:
        state.cleanup["sidecar"] = stop_source_sidecar(
            state.process,
            state.stderr_handle,
            state.sidecar_identity,
        )
        tree_cleanup = state.cleanup["sidecar"].get("tree_cleanup", {})
        if tree_cleanup.get("tree_terminated") is not True:
            state.report["status"] = "FAIL"
    except Exception as exc:
        state.cleanup["sidecar_stop_error"] = type(exc).__name__
        state.report["status"] = "FAIL"
    try:
        state.cleanup["generated_audio_removed"] = safe_remove_isolated_audio(state.runtime)
    except Exception as exc:
        state.cleanup["audio_cleanup_error"] = type(exc).__name__
        state.report["status"] = "FAIL"
    if state.model_link is not None:
        try:
            remove_models_link(state.model_link, state.models_root)
            state.cleanup["models_link_removed"] = True
        except Exception as exc:
            state.cleanup["models_link_cleanup_error"] = type(exc).__name__
            state.report["status"] = "FAIL"
    state.report["cleanup"] = state.cleanup
    state.report["finished_at"] = utc_now()


def run_live_evidence(arguments: argparse.Namespace, output: Path) -> dict[str, Any]:
    state = _create_stt_live_run(arguments, output)
    report = state.report
    try:
        _execute_stt_live_run(state)
        report["actual_run"] = True
        report["status"] = "PASS"
        return report
    except Exception as exc:
        report["actual_run"] = True
        report["status"] = "FAIL"
        report["failure"] = {
            "type": type(exc).__name__,
            # The script only creates public synthetic text; still, avoid
            # leaking temp/user paths from platform exceptions.
            "message": str(exc)
            .replace(str(state.runtime), "<isolated-runtime>")
            .replace(str(state.models_root), "<formal-model-root>"),
        }
        return report
    finally:
        _cleanup_stt_live_run(state)

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run bounded real local STT HTTP evidence with an already-installed base model, or explicitly "
            "confirm one small-model download through the formal API."
        )
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Repository-relative JSON path under build/v1400-evidence (immutable once written).",
    )
    parser.add_argument(
        "--models-root",
        type=Path,
        default=default_models_root(),
        help=(
            "Formal models root. Default mode requires base/config.json; explicitly confirmed small mode "
            "requires that no small path exists. Models are never deleted."
        ),
    )
    parser.add_argument(
        "--model-id",
        choices=(MODEL_ID, DOWNLOAD_MODEL_ID),
        default=MODEL_ID,
        help="Exercise an already-installed base or small model. Download mode always selects small.",
    )
    parser.add_argument(
        "--download-small-confirmed",
        action="store_true",
        help=(
            "Explicitly authorize the formal HTTP API to download small after proving confirmed=false is "
            "rejected. Refuses any pre-existing small path and never deletes a model."
        ),
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="After an explicitly confirmed small download, prove CPU/int8 load and unload, then stop before transcription.",
    )
    parser.add_argument(
        "--verify-download-receipt",
        type=Path,
        help=(
            "Prior successful A09 raw download report under build/v1400-evidence. No model is downloaded "
            "or deleted; the installed small model is hashed and compared to this receipt."
        ),
    )
    parser.add_argument(
        "--receipt-envelope",
        type=Path,
        help="Prior v14-evidence-runner envelope that cryptographically binds --verify-download-receipt.",
    )
    parser.add_argument(
        "--download-timeout-seconds",
        type=float,
        default=1800.0,
        help="Maximum wait for an explicitly confirmed small-model download (default: 1800).",
    )
    parser.add_argument("--startup-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--cancel-threshold-ms", type=float, default=MAX_CANCEL_MS)
    parser.add_argument(
        "--require-gpu-observation",
        action="store_true",
        help="Fail if nvidia-smi cannot observe the CPU worker's GPU-compute absence.",
    )
    args = parser.parse_args(argv)
    if args.startup_timeout_seconds <= 0:
        parser.error("--startup-timeout-seconds must be positive")
    if args.cancel_threshold_ms <= 0:
        parser.error("--cancel-threshold-ms must be positive")
    if args.download_timeout_seconds <= 0:
        parser.error("--download-timeout-seconds must be positive")
    if args.download_only and not args.download_small_confirmed:
        parser.error("--download-only requires --download-small-confirmed")
    if args.download_small_confirmed and not args.download_only:
        parser.error("--download-small-confirmed requires --download-only so A09 stays independent")
    if args.download_small_confirmed and args.model_id != MODEL_ID:
        parser.error("download mode selects small automatically; do not combine it with --model-id")
    if (args.verify_download_receipt is None) != (args.receipt_envelope is None):
        parser.error("--verify-download-receipt and --receipt-envelope must be provided together")
    if args.verify_download_receipt is not None:
        if args.download_small_confirmed or args.download_only:
            parser.error("receipt verification cannot be combined with download switches")
        if args.model_id != MODEL_ID:
            parser.error("receipt verification selects small automatically; do not combine it with --model-id")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = resolve_output_path(args.output)
    except LiveEvidenceError as exc:
        print(f"v14 STT live evidence failed before execution: {exc}", file=sys.stderr)
        return 2
    if output.exists():
        print(f"v14 STT live evidence failed before execution: refusing to overwrite {output.name}", file=sys.stderr)
        return 2
    report = run_live_evidence(args, output)
    try:
        write_json_once(output, report)
    except LiveEvidenceError as exc:
        print(f"v14 STT live evidence could not write output: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "actual_run": report["actual_run"],
                "output": output.relative_to(ROOT).as_posix(),
                "microphone_capture": "NOT_RUN",
                "model_download": report.get("scope", {}).get("model_download", "NOT_CALLED"),
                "user_confirmation": bool(report.get("scope", {}).get("user_confirmation")),
                "ollama_actions": "NONE",
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
