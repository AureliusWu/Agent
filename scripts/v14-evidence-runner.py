from __future__ import annotations

"""Run one explicit command and save a source-bound v14 execution envelope.

This runner is deliberately small and has no model, network, or version-update
behaviour.  It runs only the argv supplied after ``--`` with ``shell=False``;
therefore a model download can never be started implicitly by the evidence
tooling.

Example (while VERSION is still 13.0.0)::

    python scripts/v14-evidence-runner.py --case A01 --case A25 \
      --output executions/a01-a25-backend.json -- \
      .\\siyi\\.venv\\Scripts\\python.exe -m pytest tests/backend -q

The JSON output is written under ``build/v1400-evidence`` and is accepted by
``scripts/v14-evidence.py`` as PASS evidence only when the supplied command
exits successfully.  A non-zero exit or timeout still writes an envelope, but
it is unequivocally marked ``FAIL`` and the runner itself exits non-zero.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
TARGET_VERSION = "14.0.0"
REPORT_SCHEMA_VERSION = 5
REPORT_TYPE = "v14_execution"
RUNNER_NAME = "v14-evidence-runner"
VALID_CASE_IDS = frozenset(f"A{number:02d}" for number in range(1, 29))
MAX_ATTESTED_OUTPUT_BYTES = 16 * 1024 * 1024
INTERACTIVE_COMMAND_CASES: dict[str, frozenset[str]] = {
    "scripts/v14-desktop-voice-acceptance-evidence.py": frozenset(
        {
            "A04",
            "A05",
            "A06",
            "A12",
            "A13",
            "A14",
            "A15",
            "A16",
            "A17",
            "A18",
            "A19",
            "A24",
        }
    ),
    "scripts/v14-a23-endurance-evidence.py": frozenset({"A23"}),
    "scripts/smoke-msi.ps1": frozenset({"A28"}),
}
GENERATED_DOCUMENT_FILENAMES = frozenset(
    {
        "TEST_MATRIX.json",
        "RELEASE_STATUS.json",
        "EVIDENCE_MANIFEST.json",
        "IMPLEMENTATION_FEEDBACK.md",
    }
)


class RunnerValidationError(ValueError):
    """Raised before a command is started for an unsafe evidence request."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def normalize_version(value: object) -> str:
    version = str(value or "").strip()
    if version.startswith("v") and len(version) > 1 and version[1].isdigit():
        version = version[1:]
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise RunnerValidationError(f"unsafe version: {value!r}")
    return version


def compact_version(version: str) -> str:
    compact = re.sub(r"[^0-9A-Za-z]", "", version)
    if not compact:
        raise RunnerValidationError(f"version has no compact form: {version!r}")
    return compact


def expected_evidence_root(repository_root: Path, target_version: str) -> Path:
    return repository_root / "build" / f"v{compact_version(target_version)}-evidence"


def repository_relative(repository_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError as exc:
        raise RunnerValidationError(f"path escapes repository root: {path}") from exc


def resolve_repository_path(repository_root: Path, value: str, *, field: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise RunnerValidationError(f"{field} must be repository-relative: {value}")
    resolved = (repository_root / candidate).resolve()
    try:
        resolved.relative_to(repository_root.resolve())
    except ValueError as exc:
        raise RunnerValidationError(f"{field} escapes repository root: {value}") from exc
    return resolved


def resolve_output_path(repository_root: Path, target_version: str, value: str) -> Path:
    if not value.strip():
        raise RunnerValidationError("--output must be a non-empty path relative to the evidence root")
    candidate = Path(value)
    if candidate.is_absolute():
        raise RunnerValidationError("--output must be relative to the evidence root")
    evidence_root = expected_evidence_root(repository_root, target_version).resolve()
    output = (evidence_root / candidate).resolve()
    try:
        output.relative_to(evidence_root)
    except ValueError as exc:
        raise RunnerValidationError("--output escapes the v14 evidence root") from exc
    if output.suffix.lower() != ".json":
        raise RunnerValidationError("--output must end with .json")
    return output


def resolve_attested_output_path(repository_root: Path, target_version: str, value: str) -> Path:
    """Resolve one freshly-created raw JSON report under the evidence root."""

    output = resolve_output_path(repository_root, target_version, value)
    if os.path.lexists(output):
        raise RunnerValidationError(
            f"--attest-output must not exist before the command starts: {repository_relative(repository_root, output)}"
        )
    return output


def resolve_attested_output_paths(
    repository_root: Path,
    target_version: str,
    values: list[str],
    *,
    envelope_output: Path,
) -> list[Path]:
    """Validate fresh, non-duplicate raw-report destinations before execution."""

    resolved: list[Path] = []
    for value in values:
        path = resolve_attested_output_path(repository_root, target_version, value)
        if path == envelope_output:
            raise RunnerValidationError("--attest-output must not reuse the execution-envelope path")
        if path in resolved:
            raise RunnerValidationError("--attest-output must not repeat a path")
        resolved.append(path)
    return resolved


def git_output(repository_root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = completed.stdout.strip()
    return value or None


def git_bytes(repository_root: Path, *arguments: str) -> bytes | None:
    """Return raw Git output so the source fingerprint is byte-accurate."""

    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout


def is_generated_evidence_path(relative_text: str) -> bool:
    normalized = relative_text.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    evidence_prefix = f"build/v{compact_version(TARGET_VERSION)}-evidence"
    generated_documents = {
        f"docs/{TARGET_VERSION}/{filename}" for filename in GENERATED_DOCUMENT_FILENAMES
    }
    return (
        normalized in generated_documents
        or normalized == evidence_prefix
        or normalized.startswith(evidence_prefix + "/")
    )


def source_workspace_clean(repository_root: Path) -> bool | None:
    """Ignore generated evidence when determining whether source is clean."""

    status = git_bytes(repository_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status is None:
        return None
    entries = [entry for entry in status.split(b"\0") if entry]
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            raise RunnerValidationError("Git returned an invalid porcelain status entry")
        status_code = entry[:2]
        paths = [entry[3:]]
        if status_code[:1] in {b"R", b"C"} or status_code[1:2] in {b"R", b"C"}:
            if index >= len(entries):
                raise RunnerValidationError("Git returned an incomplete rename/copy status entry")
            paths.append(entries[index])
            index += 1
        for raw_path in paths:
            try:
                relative_text = raw_path.decode("utf-8", errors="surrogateescape")
            except UnicodeDecodeError as exc:
                raise RunnerValidationError("Git returned a non-decodable source path") from exc
            if not is_generated_evidence_path(relative_text):
                return False
    return True


def source_tree_fingerprint(repository_root: Path) -> str | None:
    """Hash tracked and untracked source while excluding generated evidence."""

    tracked = git_bytes(repository_root, "ls-files", "-z")
    untracked = git_bytes(repository_root, "ls-files", "--others", "--exclude-standard", "-z")
    if tracked is None or untracked is None:
        return None

    root = repository_root.resolve()
    digest = hashlib.sha256()
    digest.update(b"siyi-v14-source-tree-fingerprint-v1\0")
    paths = sorted({path for path in tracked.split(b"\0") + untracked.split(b"\0") if path})
    for raw_path in paths:
        try:
            relative_text = raw_path.decode("utf-8", errors="surrogateescape")
        except UnicodeDecodeError as exc:
            raise RunnerValidationError("Git returned a non-decodable source path") from exc
        if is_generated_evidence_path(relative_text):
            continue

        candidate = root / relative_text
        try:
            candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise RunnerValidationError(f"source fingerprint path escapes repository root: {relative_text}") from exc

        digest.update(len(raw_path).to_bytes(8, "big"))
        digest.update(raw_path)
        if candidate.is_symlink():
            payload = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
            kind = b"L"
        elif candidate.is_file():
            payload = candidate.read_bytes()
            kind = b"F"
        elif not candidate.exists():
            payload = b""
            kind = b"D"
        else:
            raise RunnerValidationError(f"unsupported source entry in fingerprint: {relative_text}")
        digest.update(kind)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest().upper()


def source_identity(repository_root: Path) -> dict[str, object]:
    version_path = repository_root / "VERSION"
    if not version_path.is_file():
        raise RunnerValidationError(f"VERSION is missing: {version_path}")
    source_version = normalize_version(version_path.read_text(encoding="ascii").strip())
    source_commit = git_output(repository_root, "rev-parse", "HEAD")
    if not source_commit:
        raise RunnerValidationError("a committed Git HEAD is required for executable release evidence")
    fingerprint = source_tree_fingerprint(repository_root)
    if not fingerprint:
        raise RunnerValidationError("a current source-tree fingerprint is required for executable release evidence")
    return {
        "source_version": source_version,
        "source_commit": source_commit,
        "workspace_clean": source_workspace_clean(repository_root),
        "source_tree_fingerprint": fingerprint,
    }


def normalized_case_ids(values: list[str]) -> list[str]:
    if not values:
        raise RunnerValidationError("provide at least one --case A01 through A28")
    case_ids: list[str] = []
    for value in values:
        case_id = value.strip().upper()
        if case_id not in VALID_CASE_IDS:
            raise RunnerValidationError(f"unknown v14 acceptance case: {value!r}")
        if case_id not in case_ids:
            case_ids.append(case_id)
    return case_ids


def json_command(argv: list[str]) -> str:
    """Use the Windows command rendering that operators can paste into a ledger."""

    return subprocess.list2cmdline(argv)


def relative_existing_argument(repository_root: Path, cwd: Path, value: str) -> str | None:
    """Return an existing repository path mentioned as a command argument."""

    if not value or value.startswith("-"):
        return None
    candidate = Path(value)
    roots = (cwd, repository_root)
    for base in roots:
        resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        try:
            resolved.relative_to(repository_root.resolve())
        except ValueError:
            continue
        if resolved.exists():
            return repository_relative(repository_root, resolved)
    return None


def command_contract(repository_root: Path, cwd: Path, argv: list[str]) -> dict[str, object]:
    """Describe whether an executed command is controlled release evidence.

    The runner still records arbitrary commands as ``uncontrolled``.  The
    evidence generator refuses to promote those to PASS, which preserves the
    runner's diagnostic value without accepting a one-line ``exit 0`` command
    as an acceptance test.
    """

    lowered = [value.replace("\\", "/").lower() for value in argv]
    mentioned = [
        relative
        for value in argv
        if (relative := relative_existing_argument(repository_root, cwd, value)) is not None
    ]
    paths = list(dict.fromkeys(mentioned))
    invokes_pytest = "pytest" in lowered or any(
        value == "-m" and index + 1 < len(lowered) and lowered[index + 1] == "pytest"
        for index, value in enumerate(lowered)
    )
    test_paths = [path for path in paths if path.startswith("tests/")]
    if invokes_pytest and test_paths:
        return {"kind": "pytest", "paths": test_paths}

    script_paths = [path for path in paths if path.startswith("scripts/")]
    if script_paths:
        return {"kind": "repository_script", "paths": script_paths}

    invokes_cargo_test = any(value.endswith("cargo") or value.endswith("cargo.exe") for value in lowered) and "test" in lowered
    cargo_manifest = "desktop/src-tauri/Cargo.toml"
    if invokes_cargo_test and cargo_manifest in paths:
        return {"kind": "cargo_test", "paths": [cargo_manifest]}

    invokes_npm_run = any(value.endswith("npm") or value.endswith("npm.cmd") for value in lowered) and "run" in lowered
    frontend_package = repository_root / "desktop" / "frontend" / "package.json"
    if invokes_npm_run and frontend_package.is_file() and "desktop/frontend" in " ".join(lowered):
        return {"kind": "npm_script", "paths": ["desktop/frontend/package.json"]}
    return {"kind": "uncontrolled", "paths": []}


def interactive_terminal_available() -> bool:
    """Return whether all standard streams are attached to a real terminal."""

    streams = (sys.stdin, sys.stdout, sys.stderr)
    return all(getattr(stream, "isatty", lambda: False)() for stream in streams)


def validate_interactive_request(
    *,
    case_ids: list[str],
    contract: dict[str, object],
    timeout_seconds: float,
) -> None:
    """Allow inherited terminal I/O only for operator-assisted v14 collectors."""

    if not case_ids:
        raise RunnerValidationError(
            "--interactive requires at least one operator-assisted acceptance case"
        )
    if contract.get("kind") != "repository_script":
        raise RunnerValidationError(
            "--interactive is restricted to controlled operator-assisted repository scripts"
        )
    paths = contract.get("paths")
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
        raise RunnerValidationError(
            "--interactive requires exactly one controlled operator-assisted script"
        )
    allowed_cases = INTERACTIVE_COMMAND_CASES.get(paths[0])
    if allowed_cases is None:
        raise RunnerValidationError("--interactive is not permitted for this repository script")
    disallowed_cases = sorted(set(case_ids) - allowed_cases)
    if disallowed_cases:
        raise RunnerValidationError(
            f"--interactive script {paths[0]} cannot collect cases: {', '.join(disallowed_cases)}"
        )
    if not interactive_terminal_available():
        raise RunnerValidationError(
            "--interactive requires stdin, stdout, and stderr to be attached to a visible terminal"
        )
    minimum_timeout = 3600.0 if "A23" in case_ids else 1800.0
    if timeout_seconds < minimum_timeout:
        raise RunnerValidationError(
            f"--interactive {', '.join(case_ids)} requires --timeout-seconds of at least "
            f"{minimum_timeout:g}"
        )


def safe_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileExistsError as exc:
        raise RunnerValidationError(f"refusing to overwrite immutable evidence: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Preserve an interrupted filename: a later run must create a new
        # evidence path rather than replacing uncertain output.
        raise


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare identity and content-relevant metadata across one FD read."""

    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_nlink == second.st_nlink
    )


def _read_attested_output(path: Path, *, evidence_root: Path) -> tuple[bytes, dict[str, Any]]:
    """Read one raw report through a stable regular-file descriptor.

    The path was required to be absent before command execution.  Rechecking
    its file identity after the descriptor read prevents a post-run rename or
    symlink swap from being silently attested.
    """

    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(evidence_root.resolve())
    except FileNotFoundError as exc:
        # A controlled command can fail before it writes its declared raw
        # report.  This is materially different from a path traversal and
        # keeps the execution envelope actionable without relaxing the root
        # boundary check below.
        raise RunnerValidationError("attested output was not created by the command") from exc
    except (OSError, ValueError) as exc:
        raise RunnerValidationError("attested output escaped the v14 evidence root") from exc
    if path.is_symlink() or not path.is_file():
        raise RunnerValidationError("attested output must be a regular JSON file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RunnerValidationError(f"could not read attested output: {path.name}") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise RunnerValidationError("attested output must be a regular file")
            if before.st_nlink != 1:
                raise RunnerValidationError("attested output must be a newly created, non-linked file")
            if before.st_size > MAX_ATTESTED_OUTPUT_BYTES:
                raise RunnerValidationError("attested output exceeds the bounded JSON size")
            payload_bytes = handle.read(MAX_ATTESTED_OUTPUT_BYTES + 1)
            after = os.fstat(handle.fileno())
    except BaseException:
        raise
    if len(payload_bytes) > MAX_ATTESTED_OUTPUT_BYTES:
        raise RunnerValidationError("attested output exceeds the bounded JSON size")
    try:
        path_after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise RunnerValidationError("attested output disappeared during verification") from exc
    if path.is_symlink() or not stat.S_ISREG(path_after.st_mode) or not _same_file(before, after) or not _same_file(before, path_after):
        raise RunnerValidationError("attested output changed during verification")
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerValidationError("attested output must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise RunnerValidationError("attested output must be a JSON object")
    return payload_bytes, payload


def collect_attested_outputs(
    paths: list[Path],
    *,
    repository_root: Path,
    target_version: str,
    source: dict[str, object],
) -> list[dict[str, object]]:
    """Bind freshly-created raw reports to the exact command/source identity."""

    if not paths:
        return []
    evidence_root = expected_evidence_root(repository_root, target_version).resolve()
    bound: list[dict[str, object]] = []
    for path in paths:
        payload_bytes, payload = _read_attested_output(path, evidence_root=evidence_root)
        raw_status = payload.get("status")
        raw_actual_run = payload.get("actual_run")
        if raw_status not in {"PASS", "FAIL"}:
            raise RunnerValidationError("attested output status must be PASS or FAIL")
        if not isinstance(raw_actual_run, bool):
            raise RunnerValidationError("attested output actual_run must be a boolean")
        if payload.get("target_version") != target_version:
            raise RunnerValidationError("attested output target_version does not match the envelope")
        report_type = payload.get("report_type")
        if not isinstance(report_type, str) or not report_type.strip():
            raise RunnerValidationError("attested output needs a report_type")
        producer = payload.get("producer")
        if producer is not None and (not isinstance(producer, str) or not producer.strip()):
            raise RunnerValidationError("attested output producer must be a non-empty string when provided")

        raw_source = payload.get("source")
        source_mode = "runner_verified_equivalent"
        if raw_source is not None:
            if not isinstance(raw_source, dict):
                raise RunnerValidationError("attested output source must be an object")
            for field in ("source_version", "source_commit", "source_tree_fingerprint", "workspace_clean"):
                if raw_source.get(field) != source.get(field):
                    raise RunnerValidationError(
                        f"attested output source identity does not match the envelope: {field}"
                    )
            source_mode = "raw_report"

        entry: dict[str, object] = {
            "path": repository_relative(repository_root, path),
            "sha256": sha256_bytes(payload_bytes),
            "bytes": len(payload_bytes),
            "status": raw_status,
            "actual_run": raw_actual_run,
            "target_version": target_version,
            "source_version": source["source_version"],
            "source_commit": source["source_commit"],
            "source_tree_fingerprint": source["source_tree_fingerprint"],
            "workspace_clean": source["workspace_clean"],
            "source_identity_mode": source_mode,
            "report_type": report_type.strip(),
        }
        if isinstance(producer, str):
            entry["producer"] = producer.strip()
        bound.append(entry)
    return bound


def capture_command(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    environment: dict[str, str] | None = None,
    interactive: bool = False,
) -> dict[str, object]:
    """Execute only caller-provided argv and record its terminal-I/O mode."""

    started_at = utc_now()
    started = time.perf_counter()
    timed_out = False
    execution_error: str | None = None
    exit_code: int | None = None
    stdout: bytes | None = None if interactive else b""
    stderr: bytes | None = None if interactive else b""
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            shell=False,
            check=False,
            capture_output=not interactive,
            timeout=timeout_seconds,
            env=environment,
        )
        exit_code = completed.returncode
        if not interactive:
            stdout = completed.stdout
            stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        if not interactive:
            stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
            stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
        else:
            stdout = None
            stderr = None
        execution_error = f"command timed out after {timeout_seconds:g} seconds"
    except OSError as exc:
        execution_error = f"command could not start: {exc.__class__.__name__}: {exc}"
    finished_at = utc_now()
    duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
    status = "PASS" if exit_code == 0 and not timed_out and execution_error is None else "FAIL"
    result: dict[str, object] = {
        "actual_run": execution_error is None or timed_out,
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "stdio_mode": "inherited_terminal" if interactive else "captured",
        "stdout": {
            "captured": not interactive,
            "sha256": sha256_bytes(stdout) if stdout is not None else None,
            "bytes": len(stdout) if stdout is not None else None,
        },
        "stderr": {
            "captured": not interactive,
            "sha256": sha256_bytes(stderr) if stderr is not None else None,
            "bytes": len(stderr) if stderr is not None else None,
        },
    }
    if execution_error:
        result["error"] = execution_error
    return result


def build_report(
    *,
    repository_root: Path,
    target_version: str,
    case_ids: list[str],
    argv: list[str],
    cwd: Path,
    timeout_seconds: float,
    attested_outputs: list[Path],
    interactive: bool = False,
) -> dict[str, object]:
    source = source_identity(repository_root)
    environment = os.environ.copy()
    environment["SIYI_V14_EVIDENCE_SOURCE_IDENTITY"] = json.dumps(source, ensure_ascii=False, sort_keys=True)
    execution = capture_command(
        argv,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        environment=environment,
        interactive=interactive,
    )
    bound_outputs: list[dict[str, object]] = []
    if attested_outputs:
        try:
            source_after = source_identity(repository_root)
            if source_after != source:
                raise RunnerValidationError("source identity changed while the attested command was running")
            bound_outputs = collect_attested_outputs(
                attested_outputs,
                repository_root=repository_root,
                target_version=target_version,
                source=source,
            )
            if execution["status"] == "PASS" and any(
                item["status"] != "PASS" or item["actual_run"] is not True for item in bound_outputs
            ):
                raise RunnerValidationError("a successful command produced a non-passing attested output")
        except RunnerValidationError as exc:
            execution["status"] = "FAIL"
            execution["error"] = str(exc)
    status = str(execution["status"])
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "producer": RUNNER_NAME,
        "target_version": target_version,
        **source,
        "actual_run": execution["actual_run"],
        "status": status,
        "case_ids": case_ids,
        "command": json_command(argv),
        "command_contract": command_contract(repository_root, cwd, argv),
        "cwd": repository_relative(repository_root, cwd),
        "recorded_at": execution["finished_at"],
        "execution": execution,
    }
    if attested_outputs:
        report["attested_outputs"] = bound_outputs
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an explicit command and capture a v14 release-evidence envelope."
    )
    parser.add_argument(
        "--target-version",
        default=TARGET_VERSION,
        help="Must remain 14.0.0; source VERSION may still be 13.0.0.",
    )
    parser.add_argument("--case", action="append", default=[], help="Acceptance case ID, repeatable (A01-A28).")
    parser.add_argument(
        "--output",
        required=True,
        help="JSON path relative to build/v1400-evidence, for example executions/a01.json.",
    )
    parser.add_argument(
        "--attest-output",
        action="append",
        default=[],
        help=(
            "Fresh raw JSON output relative to build/v1400-evidence. May be repeated; each path "
            "must not exist before the command and is SHA/source-bound into this envelope."
        ),
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Repository-relative working directory for the supplied command (default: repository root).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=900.0,
        help="Positive command timeout in seconds (default: 900).",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "Inherit a visible terminal for the approved operator-assisted v14 collectors only; "
            "their stdout/stderr are deliberately not captured in the envelope."
        ),
    )
    parser.add_argument("--repository-root", type=Path, default=SCRIPT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Explicit command argv after --.")
    arguments = parser.parse_args(argv)
    if arguments.command and arguments.command[0] == "--":
        arguments.command = arguments.command[1:]
    if not arguments.command:
        parser.error("provide an explicit command after --")
    if arguments.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv or sys.argv[1:])
    target_version = normalize_version(arguments.target_version)
    if target_version != TARGET_VERSION:
        raise RunnerValidationError(f"this runner is scoped to v{TARGET_VERSION}, not v{target_version}")
    repository_root = arguments.repository_root.resolve()
    output = resolve_output_path(repository_root, target_version, arguments.output)
    if os.path.lexists(output):
        raise RunnerValidationError("--output must not overwrite an existing execution envelope")
    attested_outputs = resolve_attested_output_paths(
        repository_root,
        target_version,
        [str(value) for value in arguments.attest_output],
        envelope_output=output,
    )
    cwd = resolve_repository_path(repository_root, arguments.cwd, field="--cwd")
    if not cwd.is_dir():
        raise RunnerValidationError(f"--cwd is not a directory: {cwd}")
    case_ids = normalized_case_ids(arguments.case)
    command = [str(value) for value in arguments.command]
    if arguments.interactive:
        validate_interactive_request(
            case_ids=case_ids,
            contract=command_contract(repository_root, cwd, command),
            timeout_seconds=arguments.timeout_seconds,
        )
    report = build_report(
        repository_root=repository_root,
        target_version=target_version,
        case_ids=case_ids,
        argv=command,
        cwd=cwd,
        timeout_seconds=arguments.timeout_seconds,
        attested_outputs=attested_outputs,
        interactive=arguments.interactive,
    )
    safe_write_json(output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "target_version": target_version,
                "source_version": report["source_version"],
                "path": repository_relative(repository_root, output),
                "case_ids": report["case_ids"],
                "exit_code": report["execution"]["exit_code"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerValidationError as exc:
        print(f"v14 evidence runner failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
