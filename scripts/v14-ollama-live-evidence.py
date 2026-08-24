from __future__ import annotations

"""Collect structured, source-bound A26 Ollama lifecycle evidence.

The verifier directly reuses the production-code controller from the v14
resource verifier.  It starts only an explicitly owned service on port 11435,
requires an idle runtime, preloads and unloads the already-installed qwen3:4b,
observes resource release, then stops the same owned service.  Port 11434 and
all pre-existing/unknown Ollama processes are protected and never targeted.

This script does not use pytest's process exit as evidence, so an all-skipped
test session cannot become PASS.  Every accepted probe is represented in the
raw report and is validated again by ``v14-evidence.py``.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_SUPPORT_SCRIPT = ROOT / "scripts" / "v14-resource-live-evidence.py"
EVIDENCE_RUNNER = ROOT / "scripts" / "v14-evidence-runner.py"
TARGET_VERSION = "14.0.0"
OLLAMA_URL = "http://127.0.0.1:11435"
OLLAMA_PORT = 11435
QWEN_MODEL = "qwen3:4b"
POST_UNLOAD_PROCESS_QUIESCENCE_SECONDS = 10.0
POST_UNLOAD_PROCESS_POLL_SECONDS = 0.25
TEST_NAMES = (
    "external_process_and_port_preflight",
    "test_owned_service_identity",
    "qwen3_4b_preload_and_ps",
    "qwen3_4b_unload_and_resource_release",
    "test_owned_service_stop_and_external_protection",
)


class OllamaEvidenceError(RuntimeError):
    """A live A26 invariant was not observed."""


_SUPPORT: Any | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_support() -> Any:
    global _SUPPORT
    if _SUPPORT is not None:
        return _SUPPORT
    spec = importlib.util.spec_from_file_location("v14_ollama_resource_support", RESOURCE_SUPPORT_SCRIPT)
    if spec is None or spec.loader is None:
        raise OllamaEvidenceError("could not load the reviewed v14 Ollama controller")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    required = (
        "identity_as_dict",
        "list_ollama_processes",
        "ollama_model_store_fingerprint",
        "owner_sha256",
        "run_model_controller",
        "safe_controller_result",
        "safe_service_summary",
        "successful_test_owned_stop",
        "validate_ollama_url",
    )
    if any(not hasattr(module, name) for name in required):
        raise OllamaEvidenceError("reviewed v14 Ollama controller is incomplete")
    _SUPPORT = module
    return module


def default_model_store() -> Path:
    configured = os.environ.get("OLLAMA_MODELS", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".ollama" / "models"


def resolve_output_path(value: str, *, repository_root: Path = ROOT) -> Path:
    root = repository_root.resolve()
    evidence_root = (root / "build" / "v1400-evidence").resolve()
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(evidence_root)
    except ValueError as exc:
        raise OllamaEvidenceError("--output must stay under build/v1400-evidence") from exc
    if resolved.suffix.casefold() != ".json":
        raise OllamaEvidenceError("--output must end in .json")
    return resolved


def write_json_immutable(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileExistsError as exc:
        raise OllamaEvidenceError(f"refusing to overwrite existing evidence: {path.name}") from exc
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
            raise OllamaEvidenceError("runner supplied an invalid source identity") from exc
        required = ("source_version", "source_commit", "source_tree_fingerprint")
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("workspace_clean"), bool)
            or not all(isinstance(identity.get(field), str) and identity[field] for field in required)
        ):
            raise OllamaEvidenceError("runner supplied an incomplete source identity")
        return {field: identity[field] for field in (*required, "workspace_clean")}
    try:
        spec = importlib.util.spec_from_file_location("v14_ollama_runner_identity", EVIDENCE_RUNNER)
        if spec is None or spec.loader is None:
            raise OllamaEvidenceError("could not load the v14 evidence identity helper")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return dict(module.source_identity(ROOT))
    except (OSError, ValueError) as exc:
        raise OllamaEvidenceError("could not calculate the current source identity") from exc


def loopback_port_listening(port: int = OLLAMA_PORT) -> bool:
    """Read-only preflight for an unknown listener on the test-owned port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _model_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    names = {
        str(item.get("name") or item.get("model") or "").strip()
        for item in values
        if isinstance(item, dict)
    }
    return sorted(name for name in names if name)


def _release_observed(value: Any) -> bool:
    return isinstance(value, dict) and any(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value.values()
    )


def _is_non_mutating_model_store_fingerprint(value: Any) -> bool:
    """Require a complete, non-linking whole-tree fingerprint.

    ``OLLAMA_MODELS`` supplies an existing dependency to the test-owned
    service, but does not create an operating-system read-only mount.  The
    lifecycle proof therefore relies on an explicit full regular-file-tree
    fingerprint before and after the run, with links rejected rather than
    followed.
    """

    fingerprint = value if isinstance(value, dict) else {}
    digest = fingerprint.get("whole_tree_sha256")
    count = fingerprint.get("regular_file_count")
    bytes_total = fingerprint.get("regular_file_bytes")
    return (
        fingerprint.get("mode") == "non_mutating_api_intent"
        and fingerprint.get("links_followed") is False
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
        and isinstance(bytes_total, int)
        and not isinstance(bytes_total, bool)
        and bytes_total > 0
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )


def _check(report: dict[str, Any], name: str, passed: bool, **details: Any) -> None:
    report.setdefault("checks", {})[name] = {"passed": bool(passed), **details}
    if not passed:
        raise OllamaEvidenceError(name)


def _identity_list(support: Any) -> list[Any]:
    return list(support.list_ollama_processes())


def _safe_identities(support: Any, values: list[Any]) -> list[dict[str, Any]]:
    return [support.identity_as_dict(item) for item in values]


def _owned_service_valid(service: dict[str, Any], owner_hash: str) -> bool:
    pid = service.get("managed_pid")
    return (
        service.get("status") == "MANAGED_RUNNING"
        and service.get("mode") == "managed"
        and service.get("api_healthy") is True
        and service.get("owner_sha256") == owner_hash
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and service.get("listener_pid") == pid
        and service.get("managed_port") == OLLAMA_PORT
        and service.get("managed_state_unowned") is False
    )


def _run_controller(
    support: Any,
    runtime: Path,
    *,
    action: str,
    timeout_seconds: float,
    state_path: Path,
    owner_token: str,
    model_store: Path,
    executable: str | None = None,
) -> dict[str, Any]:
    result = support.run_model_controller(
        runtime,
        action=action,
        ollama_url=OLLAMA_URL,
        timeout_seconds=timeout_seconds,
        service_state_path=state_path,
        test_owned_service_id=owner_token,
        model_store=model_store,
        executable=executable,
    )
    return support.safe_controller_result(result)


def _test_summary(test_status: dict[str, str]) -> dict[str, Any]:
    tests = [{"name": name, "status": test_status.get(name, "NOT_RUN")} for name in TEST_NAMES]
    return {
        "framework": "structured_production_lifecycle_probes",
        "collected": len(tests),
        "executed": sum(item["status"] in {"PASS", "FAIL"} for item in tests),
        "passed": sum(item["status"] == "PASS" for item in tests),
        "failed": sum(item["status"] == "FAIL" for item in tests),
        "skipped": 0,
        "tests": tests,
    }


@dataclass
class OllamaLiveState:
    arguments: argparse.Namespace
    support: Any
    model_store: Path
    owner_token: str
    owner_hash: str
    runtime: Path
    state_path: Path
    report: dict[str, Any]
    test_status: dict[str, str] = field(
        default_factory=lambda: {name: "NOT_RUN" for name in TEST_NAMES}
    )
    service_start_attempted: bool = False
    service_started: bool = False
    model_loaded: bool = False
    owned_identity: Any | None = None
    model_store_before: dict[str, Any] | None = None
    successful_work: bool = False


def _create_ollama_live_state(
    arguments: argparse.Namespace,
    output: Path,
) -> OllamaLiveState:
    support = load_support()
    model_store = arguments.model_store.expanduser().resolve()
    owner_token = str(arguments.test_owned_ollama_service_id)
    owner_hash = support.owner_sha256(owner_token)
    run_id = uuid.uuid4().hex
    runtime = output.parent / f"{output.stem}-runtime-{run_id[:12]}"
    report: dict[str, Any] = {
        "schema_version": 1,
        "report_type": "v14_ollama_live_evidence",
        "producer": "scripts/v14-ollama-live-evidence.py",
        "target_version": TARGET_VERSION,
        "recorded_at": utc_now(),
        "status": "FAIL",
        "actual_run": False,
        "source": source_identity(),
        "target": {
            "platform": "windows",
            "os_name": os.name,
            "sys_platform": sys.platform,
            "service_mode": "test_owned_managed",
            "base_url": OLLAMA_URL,
            "port": OLLAMA_PORT,
            "model": QWEN_MODEL,
            "keep_alive": "5m",
            "owner_sha256": owner_hash,
        },
        "scope": {
            "external_11434_policy": "PROTECTED_NOT_TOUCHED",
            "test_owned_11435_only": True,
            "model_store": "NON_MUTATING_API_INTENT_FULL_TREE_VERIFIED",
            "model_download": "NOT_CALLED",
            "model_delete": "NOT_CALLED",
            "chat_prompt": "NOT_SENT",
            "paid_provider_calls": "NONE",
            "stt_actions": "NONE",
            "tts_actions": "NONE",
            "microphone_capture": "NOT_RUN",
        },
        "runtime": {
            "id": run_id,
            "relative_path": runtime.relative_to(ROOT).as_posix(),
            "state_isolated": True,
            "database_isolated": True,
            "logs_isolated": True,
        },
        "checks": {},
        "results": {},
        "cleanup": {},
        "test_summary": {},
        "limitations": [
            "This verifier does not send a chat prompt or measure answer quality.",
            "It cannot prove desktop UI, installer, microphone, STT, TTS, or endurance acceptance.",
            "Any pre-existing Ollama process blocks execution and is never stopped or unloaded.",
        ],
    }
    return OllamaLiveState(
        arguments=arguments,
        support=support,
        model_store=model_store,
        owner_token=owner_token,
        owner_hash=owner_hash,
        runtime=runtime,
        state_path=runtime / "test-owned-ollama.json",
        report=report,
    )


def _preflight_ollama_live_run(state: OllamaLiveState) -> None:
    if os.name != "nt" or sys.platform != "win32":
        raise OllamaEvidenceError("A26 live evidence requires Windows")
    state.runtime.mkdir(parents=True, exist_ok=False)
    preexisting = _identity_list(state.support)
    state.report["results"]["ollama_processes_before"] = _safe_identities(
        state.support,
        preexisting,
    )
    _check(state.report, "no_preexisting_ollama_processes", not preexisting)
    port_busy = loopback_port_listening()
    state.report["results"]["test_owned_port_before"] = {
        "port": OLLAMA_PORT,
        "listening": port_busy,
    }
    _check(state.report, "test_owned_port_free_before_start", port_busy is False)
    state.test_status["external_process_and_port_preflight"] = "PASS"
    state.model_store_before = state.support.ollama_model_store_fingerprint(state.model_store)
    state.report["results"]["model_store_before"] = state.model_store_before
    _check(
        state.report,
        "non_mutating_model_store_full_tree_fingerprinted",
        _is_non_mutating_model_store_fingerprint(state.model_store_before),
    )


def _start_test_owned_ollama(state: OllamaLiveState) -> None:
    state.service_start_attempted = True
    state.report["actual_run"] = True
    started = _run_controller(
        state.support,
        state.runtime,
        action="start_test_owned_service",
        timeout_seconds=state.arguments.startup_timeout_seconds,
        state_path=state.state_path,
        owner_token=state.owner_token,
        model_store=state.model_store,
        executable=state.arguments.test_owned_ollama_executable,
    )
    service = started.get("service") if isinstance(started.get("service"), dict) else {}
    state.service_started = started.get("ok") is True and _owned_service_valid(
        service,
        state.owner_hash,
    )
    _check(state.report, "test_owned_service_identity_bound", state.service_started)
    processes = _identity_list(state.support)
    pid = int(service["managed_pid"])
    state.owned_identity = next(
        (identity for identity in processes if int(getattr(identity, "pid", 0)) == pid),
        None,
    )
    _check(
        state.report,
        "only_test_owned_ollama_after_start",
        state.owned_identity is not None and processes == [state.owned_identity],
    )
    state.report["results"]["service_start"] = {
        "controller": started,
        "process_identity": state.support.identity_as_dict(state.owned_identity),
    }
    state.test_status["test_owned_service_identity"] = "PASS"


def _preload_qwen_for_ollama_run(state: OllamaLiveState) -> None:
    preflight = _run_controller(
        state.support,
        state.runtime,
        action="preflight",
        timeout_seconds=state.arguments.controller_timeout_seconds,
        state_path=state.state_path,
        owner_token=state.owner_token,
        model_store=state.model_store,
    )
    running_before = _model_names(preflight.get("running"))
    installed = _model_names(preflight.get("installed"))
    state.report["results"]["preflight"] = preflight
    _check(
        state.report,
        "test_owned_runtime_idle_before_preload",
        preflight.get("ok") is True and running_before == [],
        running_models=running_before,
    )
    _check(
        state.report,
        "qwen3_4b_installed",
        QWEN_MODEL in installed,
        installed_models=installed,
    )
    preload = _run_controller(
        state.support,
        state.runtime,
        action="preload_qwen",
        timeout_seconds=state.arguments.controller_timeout_seconds,
        state_path=state.state_path,
        owner_token=state.owner_token,
        model_store=state.model_store,
    )
    loaded = preload.get("loaded") if isinstance(preload.get("loaded"), dict) else {}
    running_after = _model_names(preload.get("running_after"))
    state.report["results"]["preload"] = preload
    state.model_loaded = (
        preload.get("ok") is True
        and loaded.get("status") == "LOADED"
        and loaded.get("model") == QWEN_MODEL
        and loaded.get("keep_alive") == "5m"
        and running_after == [QWEN_MODEL]
    )
    _check(
        state.report,
        "qwen3_4b_preloaded",
        state.model_loaded,
        running_models=running_after,
    )
    processes = _identity_list(state.support)
    _check(
        state.report,
        "only_test_owned_ollama_after_preload",
        state.owned_identity is not None and processes == [state.owned_identity],
    )
    state.report["results"]["process_identity_after_preload"] = (
        state.support.identity_as_dict(state.owned_identity)
    )
    state.test_status["qwen3_4b_preload_and_ps"] = "PASS"
    state.successful_work = True


def _record_ollama_live_failure(state: OllamaLiveState, exc: Exception) -> None:
    state.report["failure"] = {
        "type": type(exc).__name__,
        "message": str(exc)
        .replace(str(state.runtime), "<isolated-runtime>")
        .replace(str(state.model_store), "<ollama-model-store>")[:240],
    }
    for name in TEST_NAMES:
        if state.test_status[name] == "NOT_RUN":
            state.test_status[name] = "FAIL"
            break


def _unload_test_owned_qwen(state: OllamaLiveState) -> None:
    if not state.model_loaded:
        return
    cleanup = state.report["cleanup"]
    try:
        before = _identity_list(state.support)
        cleanup["processes_before_unload"] = _safe_identities(state.support, before)
        if state.owned_identity is None or before != [state.owned_identity]:
            raise OllamaEvidenceError("test-owned process identity changed before unload")
        unload = _run_controller(
            state.support,
            state.runtime,
            action="unload_owned_qwen",
            timeout_seconds=state.arguments.controller_timeout_seconds,
            state_path=state.state_path,
            owner_token=state.owner_token,
            model_store=state.model_store,
        )
        cleanup["unload"] = unload
        unloaded = unload.get("unloaded") if isinstance(unload.get("unloaded"), dict) else {}
        running_before = _model_names(unload.get("running_before"))
        running_after = _model_names(unload.get("running_after"))
        release = unloaded.get("resource_release_observed")
        unloaded_ok = (
            unload.get("ok") is True
            and unloaded.get("status") == "UNLOADED"
            and unloaded.get("model") == QWEN_MODEL
            and QWEN_MODEL in running_before
            and QWEN_MODEL not in running_after
        )
        release_ok = _release_observed(release)
        state.report["checks"]["qwen3_4b_unloaded"] = {"passed": unloaded_ok}
        state.report["checks"]["qwen3_4b_resource_release_observed"] = {
            "passed": release_ok,
            "resource_release_observed": release,
        }
        if unloaded_ok and release_ok:
            state.model_loaded = False
            state.test_status["qwen3_4b_unload_and_resource_release"] = "PASS"
        else:
            state.report["status"] = "FAIL"
            state.test_status["qwen3_4b_unload_and_resource_release"] = "FAIL"
    except Exception as exc:
        cleanup["unload_error"] = type(exc).__name__
        state.report["checks"]["qwen3_4b_unloaded"] = {"passed": False}
        state.report["checks"]["qwen3_4b_resource_release_observed"] = {"passed": False}
        state.test_status["qwen3_4b_unload_and_resource_release"] = "FAIL"


def _wait_for_only_test_owned_ollama(
    state: OllamaLiveState,
) -> tuple[list[Any], bool, dict[str, Any]]:
    """Wait briefly for an unloaded model runner to exit without touching it.

    Ollama can retain a short-lived ``ollama.exe`` runner after ``/api/ps`` no
    longer reports the model.  Until the process set returns to the one bound
    listener, an extra executable remains ambiguous: it may be the runner, but
    it might also be an unrelated external process.  A timeout therefore fails
    the gate; it never authorizes relabeling or terminating that extra process.
    """

    started = time.monotonic()
    deadline = started + POST_UNLOAD_PROCESS_QUIESCENCE_SECONDS
    initial: list[dict[str, Any]] | None = None
    final: list[dict[str, Any]] = []
    observed: list[Any] = []
    polls = 0
    only_test_owned = False
    while True:
        observed = _identity_list(state.support)
        polls += 1
        final = _safe_identities(state.support, observed)
        if initial is None:
            initial = final
        only_test_owned = (
            state.owned_identity is not None and observed == [state.owned_identity]
        )
        if only_test_owned or time.monotonic() >= deadline:
            elapsed_ms = round((time.monotonic() - started) * 1000, 3)
            return observed, only_test_owned, {
                "maximum_wait_ms": round(POST_UNLOAD_PROCESS_QUIESCENCE_SECONDS * 1000, 3),
                "poll_interval_ms": round(POST_UNLOAD_PROCESS_POLL_SECONDS * 1000, 3),
                "poll_count": polls,
                "initial_processes": initial,
                "final_processes": final,
                "settled": only_test_owned,
                "elapsed_ms": elapsed_ms,
            }
        time.sleep(POST_UNLOAD_PROCESS_POLL_SECONDS)


def _stop_test_owned_ollama(state: OllamaLiveState) -> None:
    if not state.service_start_attempted:
        return
    cleanup = state.report["cleanup"]
    try:
        before, only_test_owned, settlement = _wait_for_only_test_owned_ollama(state)
        cleanup["post_unload_process_settlement"] = settlement
        cleanup["processes_before_stop"] = _safe_identities(state.support, before)
        cleanup["only_test_owned_before_stop"] = only_test_owned
        state.report["checks"]["only_test_owned_ollama_before_stop"] = {
            "passed": only_test_owned,
            "settlement": settlement,
        }
        stopped = _run_controller(
            state.support,
            state.runtime,
            action="stop_test_owned_service",
            timeout_seconds=state.arguments.controller_timeout_seconds,
            state_path=state.state_path,
            owner_token=state.owner_token,
            model_store=state.model_store,
        )
        cleanup["service_stop"] = stopped
        stopped_ok = state.support.successful_test_owned_stop(stopped)
        state.report["checks"]["test_owned_service_stopped"] = {"passed": stopped_ok}
        state.service_started = not stopped_ok
    except Exception as exc:
        cleanup["service_stop_error"] = type(exc).__name__
        state.report["checks"]["test_owned_service_stopped"] = {"passed": False}


def _verify_ollama_cleanup_observations(state: OllamaLiveState) -> None:
    cleanup = state.report["cleanup"]
    try:
        manifest_after = state.support.ollama_model_store_fingerprint(state.model_store)
        cleanup["model_store_after"] = manifest_after
        unchanged = state.model_store_before is not None and manifest_after == state.model_store_before
        state.report["checks"]["model_store_unchanged"] = {"passed": unchanged}
    except Exception as exc:
        cleanup["model_store_error"] = type(exc).__name__
        state.report["checks"]["model_store_unchanged"] = {"passed": False}
    try:
        final_processes = _identity_list(state.support)
        cleanup["ollama_processes_after_stop"] = _safe_identities(
            state.support,
            final_processes,
        )
        state.report["checks"]["no_ollama_process_after_stop"] = {
            "passed": final_processes == []
        }
    except Exception as exc:
        cleanup["process_inspection_error"] = type(exc).__name__
        state.report["checks"]["no_ollama_process_after_stop"] = {"passed": False}
    try:
        port_busy = loopback_port_listening()
        cleanup["test_owned_port_after_stop"] = {
            "port": OLLAMA_PORT,
            "listening": port_busy,
        }
        state.report["checks"]["test_owned_port_released"] = {
            "passed": port_busy is False
        }
    except OSError as exc:
        cleanup["port_inspection_error"] = type(exc).__name__
        state.report["checks"]["test_owned_port_released"] = {"passed": False}


def _finalize_ollama_live_report(state: OllamaLiveState) -> None:
    checks = state.report["checks"]
    stop_ok = checks.get("test_owned_service_stopped", {}).get("passed") is True
    final_ok = (
        state.successful_work
        and not state.model_loaded
        and not state.service_started
        and stop_ok
        and checks.get("model_store_unchanged", {}).get("passed") is True
        and checks.get("no_ollama_process_after_stop", {}).get("passed") is True
        and checks.get("test_owned_port_released", {}).get("passed") is True
        and all(isinstance(value, dict) and value.get("passed") is True for value in checks.values())
    )
    case = "test_owned_service_stop_and_external_protection"
    if final_ok:
        state.test_status[case] = "PASS"
    elif state.test_status[case] == "NOT_RUN":
        state.test_status[case] = "FAIL"
    state.report["test_summary"] = _test_summary(state.test_status)
    summary = state.report["test_summary"]
    state.report["status"] = (
        "PASS"
        if final_ok
        and summary["executed"] == len(TEST_NAMES)
        and summary["passed"] == len(TEST_NAMES)
        and summary["failed"] == 0
        and summary["skipped"] == 0
        else "FAIL"
    )
    state.report["finished_at"] = utc_now()


def _cleanup_ollama_live_run(state: OllamaLiveState) -> None:
    _unload_test_owned_qwen(state)
    _stop_test_owned_ollama(state)
    _verify_ollama_cleanup_observations(state)
    _finalize_ollama_live_report(state)


def run_live_evidence(arguments: argparse.Namespace, output: Path) -> dict[str, Any]:
    state = _create_ollama_live_state(arguments, output)
    try:
        _preflight_ollama_live_run(state)
        _start_test_owned_ollama(state)
        _preload_qwen_for_ollama_run(state)
    except Exception as exc:
        _record_ollama_live_failure(state, exc)
    finally:
        _cleanup_ollama_live_run(state)
    return state.report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect real test-owned Ollama A26 evidence.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--execute-live-test-owned-ollama", action="store_true")
    parser.add_argument("--test-owned-ollama-service-id")
    parser.add_argument("--model-store", type=Path, default=default_model_store())
    parser.add_argument("--test-owned-ollama-executable")
    parser.add_argument("--startup-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--controller-timeout-seconds", type=float, default=240.0)
    args = parser.parse_args(argv)
    if not args.execute_live_test_owned_ollama:
        parser.error("--execute-live-test-owned-ollama is required")
    if not isinstance(args.test_owned_ollama_service_id, str) or len(args.test_owned_ollama_service_id) < 16:
        parser.error("--test-owned-ollama-service-id must contain at least 16 characters")
    if args.startup_timeout_seconds <= 0 or args.controller_timeout_seconds <= 0:
        parser.error("timeouts must be positive")
    if not args.model_store.expanduser().is_dir() or not (args.model_store.expanduser() / "manifests").is_dir():
        parser.error("--model-store must name an existing Ollama store with manifests")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = resolve_output_path(args.output)
    except OllamaEvidenceError as exc:
        print(f"v14 Ollama live evidence failed before execution: {exc}", file=sys.stderr)
        return 2
    if output.exists():
        print(f"v14 Ollama live evidence failed before execution: refusing to overwrite {output.name}", file=sys.stderr)
        return 2
    report = run_live_evidence(args, output)
    try:
        write_json_immutable(output, report)
    except OllamaEvidenceError as exc:
        print(f"v14 Ollama live evidence could not write output: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "actual_run": report["actual_run"],
                "output": output.relative_to(ROOT).as_posix(),
                "passed": report["test_summary"].get("passed", 0),
                "failed": report["test_summary"].get("failed", 0),
                "skipped": report["test_summary"].get("skipped", 0),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
