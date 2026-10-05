"""Read-only, pre-tag RC aggregator. A ready RC is NOT a published release.

Evidence is untrusted input: require source identity, hashes, case contracts and
the specialized validators. This module never installs, invokes a model, creates
a tag, or converts a missing baseline/manual check into a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import stat
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERSION = "16.0.0"
PROTOCOL = "siyi-rc-v1"
MAX_REPORT_BYTES = 32 * 1024 * 1024
MANIFEST_FIELDS = ("manifest_version", "product_version", "git_commit", "source_fingerprint", "build_id", "database_schema_version", "component_build_ids")
COMPONENT_OBSERVERS = {"react": "react-render-runtime-v1", "tauri": "tauri-invoke-build-info-v1", "sidecar": "authenticated-sidecar-diagnostics-v1"}
EVALUATIONS = {
    "core": ("evals/tasks.json", "core", "scripted_runtime"),
    "multi_agent": ("evals/multi_agent_tasks.json", "multi_agent", "scripted_runtime"),
    "professional_agents": ("evals/professional_agent_tasks.json", "professional_agents", "scripted_runtime"),
    "adversarial": ("evals/adversarial_tasks.json", "adversarial", "adversarial"),
    "default_model": ("evals/tasks.json", "core", "live_model"),
}
ADDITIONAL_GATES = {
    "automated": (
        "frontend_desktop_tests", "frontend_build_identity", "voice_error_races",
        "file_journal_crash_matrix", "file_undo_race_matrix", "file_resource_budget",
        "database_migration_restore", "startup_performance_comparison",
    ),
    "manual": ("bulk_rename", "classify_move", "literal_replace", "restore_last_batch"),
}


class GateError(ValueError):
    pass


def module(name: str):
    spec = importlib.util.spec_from_file_location(f"rc_{name.replace('-', '_')}", ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise GateError(f"validator unavailable: {name}")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def requirement_id(category: str, name: str) -> str:
    return f"V160-{category.upper()}-{name.replace('_', '-').upper()}"


def required_gates() -> dict[str, tuple[str, ...]]:
    legacy = module("check-release-metadata").REQUIRED_RELEASE_GATES
    return {
        category: tuple(name for name in names if name != "git_tag_consistency") + ADDITIONAL_GATES.get(category, ())
        for category, names in legacy.items()
    }


def read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise GateError("report exceeds the bounded reader limit")
    value = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=lambda _: (_ for _ in ()).throw(GateError("non-finite JSON number")))
    if not isinstance(value, dict):
        raise GateError("report must be a JSON object")
    return value


def attachment(root: Path, reference: object, *, binary: bool = False) -> Any:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise GateError("attachment needs exactly path and sha256")
    relative = Path(str(reference["path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise GateError("attachment path must remain repository-relative")
    path = root / relative
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()):
        raise GateError("attachment escapes the repository")
    for item in (path, *path.parents):
        if item == root:
            break
        info = item.lstat()
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise GateError("reparse/symlink evidence is not accepted")
    expected = str(reference["sha256"])
    if re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None:
        raise GateError("attachment SHA-256 is missing or malformed")
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest().lower() != expected.lower():
        raise GateError("attachment bytes no longer match the SHA-256")
    return resolved if binary else read_json(resolved)


def executable_attachment(root: Path, reference: object) -> Path:
    path = attachment(root, reference, binary=True)
    if path.suffix.lower() != ".exe" or path.stat().st_size < 1024 * 1024:
        raise GateError("executable is missing or only a test sentinel")
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise GateError("executable does not contain a Windows PE header")
        offset = int.from_bytes(header[60:64], "little")
        if offset < 64 or offset > min(path.stat().st_size - 4, 1024 * 1024):
            raise GateError("executable PE offset is invalid")
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0":
            raise GateError("executable PE signature is invalid")
    return path


def validate_sidecar_payload(root: Path, reference: object, binary: dict) -> None:
    value = attachment(root, reference)
    if value != module("rc_payload_inventory").inventory(root, binary):
        raise GateError("actual onedir sidecar payload differs from its complete inventory")


def installer_binary_hashes(root: Path, binaries: dict, kind: str) -> dict[str, str]:
    """Derive only the SDK's fixed type-marker substitution; never ignore bytes.

    tauri-utils 2.9.3 platform.rs retains one unknown marker. The CLI replaces
    its final three bytes for NSIS/MSI, then restores the build cache. Installed
    binaries must hash to this exact derived stream. Signing or any other
    mutation is not covered and needs separately accepted artifacts.
    """
    markers = {"NSIS": b"__TAURI_BUNDLE_TYPE_VAR_NSS", "MSI": b"__TAURI_BUNDLE_TYPE_VAR_MSI"}
    if kind not in markers:
        raise GateError("unsupported installer binary variant")
    desktop = executable_attachment(root, binaries.get("desktop"))
    executable_attachment(root, binaries.get("sidecar"))
    limit = 128 * 1024 * 1024
    if desktop.stat().st_size > limit:
        raise GateError("desktop exceeds the bounded installer identity derivation")
    with desktop.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit or hashlib.sha256(content).hexdigest() != binaries["desktop"]["sha256"].lower():
        raise GateError("accepted desktop changed during installer identity derivation")
    original = b"__TAURI_BUNDLE_TYPE_VAR_UNK"
    if content.count(original) != 1:
        raise GateError("accepted desktop requires one unambiguous SDK bundle marker")
    return {"desktop": hashlib.sha256(content.replace(original, markers[kind], 1)).hexdigest(),
            "sidecar": binaries["sidecar"]["sha256"].lower()}


def artifact_binary_hashes(raw: dict, portable: dict[str, str] | None, installers: dict[str, dict[str, str]] | None) -> dict[str, str]:
    variant = raw.get("artifact_variant", "portable")
    if variant == "portable" and portable:
        return portable
    if isinstance(variant, str) and variant in {"nsis", "msi"} and installers and variant in installers:
        return installers[variant]
    raise GateError("desktop/manual evidence has an unsupported or unbound artifact variant")


def validate_component_observations(root: Path, bundle: dict[str, Any], current: dict[str, Any], build_manifest: dict[str, Any]) -> None:
    binaries = bundle.get("binaries", {})
    hashes = {key: str(binaries.get(key, {}).get("sha256", "")).lower() for key in ("desktop", "sidecar")}
    for binary in ("desktop", "sidecar"):
        executable_attachment(root, binaries.get(binary))
    validate_sidecar_payload(root, bundle.get("sidecar_payload"), binaries["sidecar"])
    observations = bundle.get("component_observations")
    if not isinstance(observations, dict) or set(observations) != set(COMPONENT_OBSERVERS):
        raise GateError("three actual component runtime observations are required; adjacent manifests are not binary identity evidence")
    for component, protocol in COMPONENT_OBSERVERS.items():
        raw = attachment(root, observations[component])
        bound_source(raw, current)
        if (raw.get("schema_version") != 1 or raw.get("report_type") != "rc_component_observation"
                or raw.get("target_version") != VERSION or raw.get("status") != "PASS" or raw.get("actual_run") is not True
                or raw.get("component") != component or raw.get("observer_protocol") != protocol
                or raw.get("build_id") != build_manifest.get("build_id") or raw.get("binary_sha256") != hashes):
            raise GateError(f"{component} runtime observation is not bound to this executable pair")
        observation = attachment(root, raw.get("observation"))
        if raw.get("kind") == "automated":
            expected = desktop_sample_command(binaries, raw.get("observation", {}).get("path"), observation)
            if (not python_command_matches(raw.get("command"), expected) or raw.get("cwd") != "."
                    or raw.get("exit_code") != 0 or raw.get("timed_out") is not False):
                raise GateError(f"{component} observation was not captured by the fixed desktop collector")
            validate_desktop_observation(observation, current=current, hashes=hashes, build_id=build_manifest.get("build_id"))
            if observation.get("sidecar_payload") != bundle.get("sidecar_payload"):
                # Different attachment filenames are valid only for the same
                # actual complete content, never a different internal payload.
                if attachment(root, observation.get("sidecar_payload")) != attachment(root, bundle.get("sidecar_payload")):
                    raise GateError("component observation used a different sidecar payload")
            manifest = observation["application_receipt"]["components"][component]
        else:
            if raw.get("kind") != "manual" or raw.get("operator_attested") is not True or not str(raw.get("operator") or "").strip():
                raise GateError(f"{component} observation requires actual desktop capture or operator attestation")
            if (observation.get("component") != component or observation.get("observer_protocol") != protocol
                    or observation.get("embedded") is not True or observation.get("isolated_test_data") is not True
                    or observation.get("binary_sha256") != hashes):
                raise GateError(f"{component} raw observation is missing embedded/runtime/binary identity")
            manifest = observation.get("manifest")
        if not isinstance(manifest, dict) or any(manifest.get(field) != build_manifest.get(field) for field in MANIFEST_FIELDS):
            raise GateError(f"{component} observed manifest disagrees with the locked release manifest")
        if manifest.get("build_type") != "Release" or manifest.get("workspace_state") != "CLEAN":
            raise GateError(f"{component} observed build is not a clean Release")


def python_command_matches(command: object, expected: list[str]) -> bool:
    return (isinstance(command, list) and bool(command) and all(isinstance(part, str) for part in command)
            and Path(command[0]).name.lower().removesuffix(".exe") in {"python", "python3"}
            and command[1:] == expected[1:])


def desktop_sample_command(binaries: dict, output: str, observation: dict) -> list[str]:
    return ["python", "scripts/record-rc-desktop-startup.py", "--desktop", binaries["desktop"]["path"],
            "--sidecar", binaries["sidecar"]["path"], "--output", output,
            "--cache-state", observation.get("cache_state"), "--startup-path", observation.get("startup_path")]


def validate_desktop_observation(observation: dict, *, current: dict, hashes: dict, build_id: str) -> None:
    if (observation.get("report_type") != "rc_desktop_startup_observation"
            or observation.get("status") != "PASS" or observation.get("actual_run") is not True
            or observation.get("desktop_render_ready") is not True or observation.get("sidecar_ready") is not True
            or observation.get("isolated_test_data") is not True or observation.get("binary_sha256") != hashes
            or observation.get("build_id") != build_id or observation.get("measurement_object") != "desktop"
            or observation.get("measurement_protocol") != "desktop-render-ready-v1"):
        raise GateError("actual desktop render observation is missing or bound to a different executable pair")
    bound_source(observation, current)
    receipt = observation.get("application_receipt")
    if (not isinstance(receipt, dict) or receipt.get("report_type") != "rc_desktop_runtime_observation"
            or receipt.get("protocol_version") != "desktop-render-ready-v1" or receipt.get("actual_run") is not True
            or receipt.get("status") != "PASS" or receipt.get("isolated_test_data") is not True
            or receipt.get("desktop_render_ready") is not True or receipt.get("sidecar_ready") is not True
            or not receipt.get("acceptance_nonce")):
        raise GateError("the native application's actual render/identity receipt is required")
    identities = receipt.get("components")
    if not isinstance(identities, dict) or set(identities) != set(COMPONENT_OBSERVERS):
        raise GateError("desktop receipt must contain all three actually observed component manifests")
    expected = identities["tauri"]
    for component, manifest in identities.items():
        if (not isinstance(manifest, dict) or manifest.get("build_id") != build_id
                or manifest.get("product_version") != current["source_version"] or manifest.get("git_commit") != current["source_commit"]
                or manifest.get("source_fingerprint") != current["source_tree_fingerprint"]
                or manifest.get("workspace_state") != "CLEAN" or manifest.get("build_type") != "Release"
                or any(manifest.get(field) != expected.get(field) for field in MANIFEST_FIELDS)):
            raise GateError(f"{component} runtime receipt does not match the measured clean build")


def validate_backend_evidence(root: Path, raw: dict[str, Any], name: str, *, all_backend_checks: bool = False) -> dict[str, Any]:
    references = raw.get("test_results")
    if not isinstance(references, dict) or set(references) != {"junit", "coverage", "collection", "execution"}:
        raise GateError("full test command requires hashed execution, JUnit, collection and coverage attachments")
    try:
        validator = module("rc_test_evidence")
        execution = attachment(root, references["execution"])
        result_directory = attachment(root, references["execution"], binary=True).parent
        validator.validate_execution(root, result_directory, execution)
        if execution.get("raw_results") != {key: references[key] for key in ("junit", "coverage", "collection")}:
            raise GateError("backend execution receipt describes different raw results")
        coverage = attachment(root, references["coverage"])
        if execution.get("protocol_version") == validator.EXECUTION_PROTOCOL:
            validator.validate_portable_coverage(coverage)
        junit = attachment(root, references["junit"], binary=True)
        collection = attachment(root, references["collection"])
        names = ("python_full_tests", *validator.CRITICAL_FILES) if all_backend_checks else (name,)
        summaries = {gate: validator.validate_raw_results(root, junit, coverage, collection, gate) for gate in names}
        return summaries.get(name, summaries[names[0]])
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise GateError(str(exc)) from exc


def valid_identity(identity: object) -> bool:
    return (isinstance(identity, dict)
            and bool(re.fullmatch(r"[0-9a-f]{40,64}", str(identity.get("source_commit", ""))))
            and bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(identity.get("source_tree_fingerprint", ""))))
            and isinstance(identity.get("workspace_clean"), bool)
            and bool(re.fullmatch(r"\d+\.\d+\.\d+", str(identity.get("source_version", "")))))


def bound_source(payload: dict[str, Any], current: dict[str, Any] | None) -> None:
    configuration = payload.get("configuration", {})
    if not isinstance(configuration, dict):
        raise GateError("configuration must be an object")
    source = payload.get("source", configuration.get("source_identity"))
    after = payload.get("source_after", configuration.get("source_identity_after"))
    if not valid_identity(source) or after != source:
        raise GateError("missing/stale before-and-after source binding")
    if current is not None and source != current:
        raise GateError("evidence belongs to a different source/version/fingerprint")


def automated_command(raw: dict[str, Any], name: str, *, root: Path | None = None) -> None:
    """A command which merely exits zero must not become safety evidence."""
    command = raw.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(part, str) for part in command):
        raise GateError("command must contain the executed argv, not a narrative")
    if "command_protocol" in raw:
        # This protocol describes the real Python script argv, not native argv.
        # It is scoped to this one collector; old native receipts stay strict.
        comparison = raw.get("performance_comparison")
        interpreter = raw.get("interpreter")
        if (name != "startup_performance_comparison" or raw.get("command_protocol") != "python-script-argv-v1"
                or not isinstance(comparison, dict) or raw.get("cwd") != "."
                or command != performance_collection_arguments(comparison)
                or not isinstance(interpreter, dict)
                or set(interpreter) != {"implementation", "version", "launcher_sha256", "runtime_sha256"}
                or interpreter.get("implementation") != "cpython"):
            raise GateError("typed Python script argv is not this exact desktop performance collector")
        version = interpreter.get("version")
        if (not isinstance(version, list) or len(version) != 3 or any(type(part) is not int for part in version)
                or version[:2] != [3, 12] or not 0 <= version[2] < 1000
                or any(not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in (
                    interpreter.get("launcher_sha256"), interpreter.get("runtime_sha256"), raw.get("process_argv_sha256")))):
            raise GateError("typed Python script argv has invalid interpreter or native-argv commitments")
        return
    program = Path(command[0]).name.lower().removesuffix(".exe").removesuffix(".cmd")
    arguments = tuple(part.replace("\\", "/") for part in command[1:])
    full_gate = program in {"powershell", "pwsh"} and arguments == ("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/test.ps1") and raw.get("cwd") == "."
    special = {"local_model_benchmark_basic", "startup_performance_comparison"}
    if full_gate and name not in special:
        return
    if program in {"python", "python3"} and raw.get("cwd") == "siyi":
        validator = module("rc_test_evidence")
        backend = {"python_full_tests", "coverage_80", *validator.CRITICAL_FILES}
        if name in backend:
            evidence_root = ROOT if root is None else root
            references = raw.get("test_results")
            if not isinstance(references, dict):
                raise GateError("controlled backend command requires hashed execution evidence")
            execution = attachment(evidence_root, references.get("execution"))
            if (type(execution.get("schema_version")) is not int or execution.get("schema_version") != 2
                    or execution.get("protocol_version") != validator.EXECUTION_PROTOCOL
                    or command != execution.get("command") or execution.get("cwd") != "siyi"):
                raise GateError("independent backend command must exactly match its fixed v2 execution receipt")
            validate_backend_evidence(evidence_root, raw, name, all_backend_checks=True)
            return
    frontend = {
        "frontend_lint": "lint", "frontend_build": "build", "frontend_security_tests": "test:security",
        "frontend_desktop_tests": "test:desktop", "frontend_build_identity": "test:build-info", "voice_error_races": "test:voice-errors",
    }
    if name in frontend and program == "npm" and arguments == ("run", frontend[name]) and raw.get("cwd") == "desktop/frontend":
        return
    if name == "version_consistency" and program in {"python", "python3"} and arguments == ("scripts/check-release-metadata.py",) and raw.get("cwd") == ".":
        return
    if name == "local_model_benchmark_basic" and raw.get("qualification_report"):
        # The aggregate gate independently validates that exact qualification,
        # including actual Runtime/model samples. This record cannot replace it.
        if program in {"python", "python3"} and arguments[:2] == ("-m", "app.evals.local_model_benchmark.runtime_file_cli") and "local_live" in arguments:
            return
    if name == "startup_performance_comparison" and raw.get("performance_comparison"):
        comparison = raw["performance_comparison"]
        expected = performance_collection_arguments(comparison)
        if program in {"python", "python3"} and arguments == tuple(expected) and raw.get("cwd") == ".":
            return
    raise GateError("executed command does not cover this mandatory requirement")


def performance_collection_arguments(value: dict[str, Any]) -> list[str]:
    """A desktop collector is distinct from component-only smoke-sidecar."""
    baseline, candidate = value.get("baseline", {}), value.get("candidate", {})
    return ["scripts/record-rc-performance.py", "--measurement-object", "desktop", "--baseline",
            str(baseline.get("binaries", {}).get("desktop", {}).get("path", "")), "--candidate",
            str(candidate.get("binaries", {}).get("desktop", {}).get("path", "")), "--output", str(value.get("output", "")),
            "--startup-path", str(candidate.get("startup_path", "")), "--cache-state", str(candidate.get("cache_state", ""))]


def performance_comparison(value: object, build_id: str) -> None:
    if not isinstance(value, dict):
        raise GateError("missing performance comparison")
    baseline, candidate = value.get("baseline"), value.get("candidate")
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise GateError("performance requires baseline and candidate observations")
    if not baseline.get("run_id") or baseline.get("run_id") == candidate.get("run_id") or candidate.get("build_id") != build_id:
        raise GateError("performance baseline cannot be the candidate itself")
    if baseline.get("build_id") == candidate.get("build_id"):
        raise GateError("performance baseline must describe a different frozen build")
    if (candidate.get("measurement_object") != "desktop" or baseline.get("measurement_object") != "desktop"
            or candidate.get("measurement_protocol") != "desktop-render-ready-v1"
            or candidate.get("startup_path") not in {"installed-nsis", "installed-msi", "portable-desktop"}
            or candidate.get("cache_state") not in {"warm", "cold"}):
        raise GateError("desktop startup requires actual desktop render readiness; sidecar health is only component evidence")
    for field in ("host_fingerprint", "startup_path", "cache_state", "measurement_protocol"):
        if not baseline.get(field) or baseline.get(field) != candidate.get(field):
            raise GateError(f"performance comparison identity mismatch: {field}")
    medians = []
    for item in (baseline, candidate):
        samples = item.get("samples_ms")
        if not isinstance(samples, list) or len(samples) < 5 or any(isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) or n <= 0 for n in samples):
            raise GateError("performance requires at least five positive finite samples per run")
        medians.append(statistics.median(samples))
    regression = medians[1] / medians[0] - 1
    if regression > .20 or (regression > .10 and not str(value.get("explanation") or "").strip()):
        raise GateError("startup regression exceeds 20%, or exceeds 10% without an explanation")


def validate_performance_evidence(root: Path, value: dict[str, Any], current: dict[str, Any], binary_hashes: dict[str, str]) -> None:
    seen_samples: set[str] = set()
    seen_observations: set[str] = set()
    seen_nonces: set[str] = set()
    run_ids: set[str] = set()
    payloads: set[tuple[str, str]] = set()
    baseline_hashes: dict[str, str] | None = None
    for label in ("baseline", "candidate"):
        series = value.get(label, {})
        references = series.get("sample_reports")
        if not isinstance(references, list) or len(references) < 5 or len(references) != len(series.get("samples_ms", [])):
            raise GateError("performance requires one raw sample attachment per observation")
        binaries = series.get("binaries")
        if not isinstance(binaries, dict) or set(binaries) != {"desktop", "sidecar"}:
            raise GateError("desktop startup requires both executable attachments")
        series_hashes = {}
        for key in ("desktop", "sidecar"):
            executable_attachment(root, binaries[key])
            series_hashes[key] = str(binaries[key]["sha256"]).lower()
        if label == "candidate" and series_hashes != binary_hashes:
            raise GateError("performance candidate binaries differ from the RC build")
        if label == "baseline":
            baseline_hashes = series_hashes
        elif series_hashes == baseline_hashes:
            raise GateError("performance baseline cannot reuse candidate binary bytes")
        for index, reference in enumerate(references):
            raw = attachment(root, reference)
            digest = str(reference["sha256"]).lower()
            if digest in seen_samples:
                raise GateError("performance repeated a raw sample instead of independent starts")
            seen_samples.add(digest)
            if raw.get("report_type") != "rc_startup_sample" or raw.get("actual_run") is not True or raw.get("status") != "PASS":
                raise GateError("wrong performance sample evidence layer")
            run_id = raw.get("run_id")
            if not isinstance(run_id, str) or not run_id or run_id in run_ids:
                raise GateError("performance sample run ID is missing or repeated")
            run_ids.add(run_id)
            if raw.get("series_run_id") != series.get("run_id") or raw.get("sample_index") != index:
                raise GateError("performance sample belongs to a different series or ordinal")
            bound_source(raw, current if label == "candidate" else None)
            if raw.get("source", {}).get("workspace_clean") is not True:
                raise GateError("performance source is not clean")
            for field in ("host_fingerprint", "startup_path", "cache_state", "measurement_protocol", "build_id"):
                if raw.get(field) != series.get(field):
                    raise GateError(f"performance sample identity mismatch: {field}")
            if raw.get("binary_sha256") != series_hashes or raw.get("exit_code") != 0 or raw.get("timed_out") is not False:
                raise GateError("performance sample process/binary binding is invalid")
            command = raw.get("command")
            expected = desktop_sample_command(binaries, raw.get("observation", {}).get("path"), series)
            if not python_command_matches(command, expected) or raw.get("cwd") != ".":
                raise GateError("unrelated command is not a startup sample")
            observation = attachment(root, raw.get("observation"))
            payload_reference = observation.get("sidecar_payload", {})
            payload_key = (str(payload_reference.get("sha256", "")), series_hashes["sidecar"])
            if payload_key not in payloads:
                validate_sidecar_payload(root, payload_reference, binaries["sidecar"])
                payloads.add(payload_key)
            observation_hash = str(raw["observation"]["sha256"]).lower()
            nonce = str(observation.get("application_receipt", {}).get("acceptance_nonce", ""))
            if observation_hash in seen_observations or nonce in seen_nonces:
                raise GateError("performance reused an observation or launch nonce instead of independent starts")
            seen_observations.add(observation_hash)
            seen_nonces.add(nonce)
            validate_desktop_observation(observation, current=raw["source"], hashes=series_hashes, build_id=series.get("build_id"))
            if observation.get("readiness_ms") != series["samples_ms"][index]:
                raise GateError("performance raw observation does not match the claimed sample")
            bound_source(observation, raw["source"])
            for field in ("host_fingerprint", "startup_path", "cache_state", "measurement_protocol", "measurement_object"):
                if observation.get(field) != series.get(field):
                    raise GateError(f"desktop observation identity mismatch: {field}")


def evaluation_pair(root: Path, pair: object, key: str, current: dict[str, Any]) -> None:
    from app.evals.comparison import compare_reports, evaluate_gate, load_policy
    from app.evals.contracts import task_contract
    from app.evals.loader import load_tasks
    from app.evals.models import EvalReport

    if not isinstance(pair, dict) or set(pair) != {"candidate", "baseline"}:
        raise GateError("both candidate and a distinct comparable baseline are mandatory")
    candidate_data = attachment(root, pair["candidate"])
    baseline_data = attachment(root, pair["baseline"])
    def evaluation_view(value):
        if value.get("report_type") == "rc_eval_public_evidence" or value.get("public_protocol") == "eval-public-v1":
            return module("rc_eval_public").as_evaluation_view(value)
        return EvalReport.model_validate(value)

    candidate = evaluation_view(candidate_data)
    baseline = evaluation_view(baseline_data)
    bound_source(candidate.model_dump(mode="json"), current)
    bound_source(baseline.model_dump(mode="json"), None)
    path, suite, mode = EVALUATIONS[key]
    result = evaluate_gate(
        candidate, load_policy(root / "evals/gate-policy.json"), compare_reports(baseline, candidate),
        expected_contract=task_contract(load_tasks(root / path, suite=suite), suite),
        expected_mode=mode, expected_version=VERSION,
    )
    if not result["passed"]:
        raise GateError("; ".join(result["failures"]))
    if mode == "live_model" and (candidate.metrics.get("model_call_count", 0) <= 0 or candidate.provider.get("name") == "deterministic-script"):
        raise GateError("default-model qualification needs actual autonomous-model calls")


def validate_matrix(root: Path, matrix: dict[str, Any], current: dict[str, Any], build_id: str, *, binary_hashes: dict[str, str] | None = None, payload_hash: str | None = None, installer_hashes: dict[str, dict[str, str]] | None = None) -> list[str]:
    errors = module("check-release-metadata")._release_gate_checks(matrix, require_tag=False)
    if matrix.get("target_version") != VERSION or matrix.get("protocol_version") != PROTOCOL:
        errors.append("matrix target/protocol is not the v16 contract")
    groups = matrix.get("release_gates", {})
    for category, names in required_gates().items():
        records = groups.get(category, []) if isinstance(groups, dict) else []
        for name in names:
            identifier = requirement_id(category, name)
            matches = [item for item in records if isinstance(item, dict) and item.get("id") == name] if isinstance(records, list) else []
            if len(matches) != 1:
                errors.append(f"{identifier}: missing/duplicate mandatory gate")
                continue
            record = matches[0]
            if record.get("requirement_id") != identifier or record.get("status") != "PASS":
                errors.append(f"{identifier}: no matching passing requirement")
                continue
            accepted = False
            for entry in record.get("evidence", []):
                if not isinstance(entry, dict) or entry.get("kind") != category or entry.get("actual_run") is not True or entry.get("outcome") != "PASS":
                    continue
                try:
                    raw = attachment(root, entry.get("report"))
                    bound_source(raw, current)
                    if raw.get("report_type") != "rc_check_evidence" or raw.get("target_version") != VERSION or raw.get("actual_run") is not True or raw.get("status") != "PASS" or raw.get("kind") != category:
                        raise GateError("wrong evidence type/layer/status")
                    checks = raw.get("checks", {})
                    if not isinstance(checks, dict) or checks.get(identifier) is not True:
                        raise GateError("unrelated evidence cannot satisfy this requirement")
                    if category in {"desktop", "manual"} and raw.get("build_id") != build_id:
                        raise GateError("desktop/manual evidence is not bound to this binary build")
                    if category in {"desktop", "manual"} and raw.get("binary_sha256") != artifact_binary_hashes(raw, binary_hashes, installer_hashes):
                        raise GateError("desktop/manual evidence lacks the exact executable hashes")
                    if category in {"desktop", "manual"} and (not payload_hash or raw.get("sidecar_payload_sha256") != payload_hash):
                        raise GateError("desktop/manual evidence lacks the exact complete sidecar payload hash")
                    if category == "manual" and (raw.get("operator_attested") is not True or not raw.get("operator") or entry.get("operator_attested") is not True):
                        raise GateError("manual acceptance needs a real operator attestation")
                    if category == "automated" and (raw.get("exit_code") != 0 or raw.get("timed_out") is not False or not raw.get("command")):
                        raise GateError("automated evidence needs an actual successful command")
                    if category == "automated":
                        automated_command(raw, name, root=root)
                        if any(str(part).replace("\\", "/") == "scripts/test.ps1" for part in raw["command"]):
                            validate_backend_evidence(root, raw, name)
                        if name == "startup_performance_comparison":
                            performance_comparison(raw.get("performance_comparison"), build_id)
                            validate_performance_evidence(root, raw["performance_comparison"], current, binary_hashes or {})
                    accepted = True
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append(f"{identifier}: {exc}")
            if not accepted:
                errors.append(f"{identifier}: no current source-bound specialized evidence")
    return errors


def check_bundle(root: Path, bundle: dict[str, Any], *, current: dict[str, Any], build_manifest: dict[str, Any], model_identity: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, str]] = []

    def check(identifier: str, action) -> None:
        try:
            action()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as exc:
            results.append({"requirement_id": identifier, "status": "FAIL", "detail": str(exc)})
        else:
            results.append({"requirement_id": identifier, "status": "PASS"})

    def source_check() -> None:
        if bundle.get("schema_version") != 1 or bundle.get("protocol_version") != PROTOCOL or bundle.get("target_version") != VERSION:
            raise GateError("bundle target/protocol is not the v16 RC contract")
        if not valid_identity(current) or current.get("workspace_clean") is not True or current.get("source_version") != VERSION or bundle.get("source") != current:
            raise GateError("RC requires current clean v16 source; a dirty development run is not an RC")
        bound_source(bundle, current)

    def component_check() -> None:
        for component in ("react", "tauri", "sidecar"):
            value = attachment(root, bundle.get("component_manifests", {}).get(component))
            for field in MANIFEST_FIELDS:
                if value.get(field) != build_manifest.get(field):
                    raise GateError(f"{component} manifest mismatch: {field}")
            if value.get("workspace_state") != "CLEAN" or value.get("build_type") != "Release":
                raise GateError(f"{component} is not a clean Release component")
        validate_component_observations(root, bundle, current, build_manifest)

    def matrix_check() -> None:
        matrix = attachment(root, bundle.get("matrix"))
        bound_source(matrix, current)
        hashes = {key: str(bundle.get("binaries", {}).get(key, {}).get("sha256", "")).lower() for key in ("desktop", "sidecar")}
        variants = {kind.lower(): installer_binary_hashes(root, bundle.get("binaries", {}), kind) for kind in ("NSIS", "MSI")}
        errors = validate_matrix(root, matrix, current, str(build_manifest["build_id"]), binary_hashes=hashes,
                                 payload_hash=attachment(root, bundle.get("sidecar_payload")).get("payload_content_sha256"), installer_hashes=variants)
        if errors:
            raise GateError("; ".join(errors))

    def installer_check() -> None:
        validator = module("check-release-evidence")
        hashes = {key: str(bundle.get("binaries", {}).get(key, {}).get("sha256", "")).lower() for key in ("desktop", "sidecar")}
        payload_hash = attachment(root, bundle.get("sidecar_payload")).get("payload_content_sha256")
        for kind in ("NSIS", "MSI"):
            expected_hashes = installer_binary_hashes(root, bundle.get("binaries", {}), kind)
            raw = attachment(root, bundle.get("installers", {}).get(kind.lower()))
            validator._validate_one(raw, root=root, version=VERSION, head=str(current["source_commit"]), kind=kind)
            source = raw["source"]
            if source.get("source_tree_fingerprint") != current["source_tree_fingerprint"] or source.get("build_id") != build_manifest["build_id"]:
                raise GateError(f"{kind} does not describe the current build")
            if raw.get("source_after") != source:
                raise GateError(f"{kind} installer source changed or lacks after-run identity")
            if raw.get("binary_sha256") != expected_hashes or raw.get("sidecar_payload_sha256") != payload_hash:
                raise GateError(f"{kind} installed executables/payload differ from the actually accepted RC")
            installed_payload = raw.get("installed_sidecar_payload")
            if (not isinstance(installed_payload, dict) or installed_payload.get("binary", {}).get("sha256") != hashes["sidecar"]
                    or module("rc_payload_inventory").content_summary(installed_payload) != payload_hash):
                raise GateError(f"{kind} actual installed sidecar payload inventory is missing or inconsistent")

    def qualification_check() -> None:
        from app.evals.local_model_benchmark.runtime_qualification import validate_qualification_report
        raw = attachment(root, bundle.get("runtime_file_qualification"))
        bound_source(raw, current)
        errors = validate_qualification_report(raw, current_identity=model_identity, target_version=VERSION)
        if errors:
            raise GateError("; ".join(errors))

    check("V160-Q01-SOURCE", source_check)
    check("V160-Q01-COMPONENTS", component_check)
    check("V160-Q01-MATRIX", matrix_check)
    for key in EVALUATIONS:
        check(f"V160-Q01-EVAL-{key.upper()}", lambda key=key: evaluation_pair(root, bundle.get("evaluations", {}).get(key), key, current))
    check("V160-P01-RUNTIME-FILE-QUALIFICATION", qualification_check)
    check("V160-Q01-INSTALLERS", installer_check)
    passed = all(item["status"] == "PASS" for item in results)
    return {"schema_version": 1, "protocol_version": PROTOCOL, "target_version": VERSION,
            "scope": "pre_tag_rc_only", "status": "RC_READY_NOT_RELEASED" if passed else "BLOCKED",
            "passed": passed, "published": False, "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--model-identity", type=Path, help="public effective identity of the designated default local model")
    parser.add_argument("--print-contract", action="store_true")
    args = parser.parse_args(argv)
    if args.print_contract:
        print(json.dumps({"target_version": VERSION, "protocol_version": PROTOCOL, "evaluations": EVALUATIONS,
                          "requirements": {kind: [requirement_id(kind, name) for name in names] for kind, names in required_gates().items()}}, indent=2))
        return 0
    if args.bundle is None or args.model_identity is None:
        parser.error("--bundle and --model-identity are required; no model will be contacted")
    sys.path.insert(0, str(ROOT / "siyi"))
    try:
        builder = module("generate_build_info")
        result = check_bundle(ROOT, read_json(args.bundle), current=builder._release_source_identity(ROOT),
                              build_manifest=builder.generate_manifest(ROOT, "Release"), model_identity=read_json(args.model_identity))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "BLOCKED", "passed": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
