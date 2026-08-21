from __future__ import annotations

"""Measure the v14 desktop-sidecar cold-start contract on the current machine.

The v14 plan deliberately has a stricter contract than the older generic
performance gate: three independent candidate starts for the median, ten for
the P95, and an on-machine v13 baseline comparison capped at 10 percent
regression.  The command does not build, download models, alter VERSION, or
touch the user's application data.  It only starts explicitly supplied frozen
sidecars through the existing isolated smoke harness.

This report is a *component* artifact.  It becomes release evidence only when
``v14-evidence-runner.py`` captures this command in a schema-v2 envelope.
"""

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TARGET_VERSION = "14.0.0"
MEDIAN_SAMPLE_COUNT = 3
P95_SAMPLE_COUNT = 10
BASELINE_SAMPLE_COUNT = 3
MEDIAN_LIMIT_MS = 2_000
P95_LIMIT_MS = 3_000
MAX_REGRESSION_RATIO = 0.10


class SidecarPerformanceError(RuntimeError):
    """Raised when a sidecar sample cannot be treated as a real measurement."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().upper()


def repository_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise SidecarPerformanceError(f"path escapes repository root: {path}") from exc


def controlled_binary(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    repository_relative(path)
    if not path.is_file() or path.is_symlink():
        raise SidecarPerformanceError(f"sidecar binary is missing or unsafe: {path}")
    return path


def controlled_output(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    evidence_root = (ROOT / "build" / "v1400-evidence").resolve()
    try:
        path.relative_to(evidence_root)
    except ValueError as exc:
        raise SidecarPerformanceError(
            "--output must remain under build/v1400-evidence"
        ) from exc
    if path.suffix.lower() != ".json":
        raise SidecarPerformanceError("--output must end in .json")
    return path


def support_payload(binary: Path) -> dict[str, Any]:
    """Return a deterministic manifest for an onedir sidecar's native payload."""

    directory = binary.parent / "_internal"
    if not directory.is_dir() or directory.is_symlink():
        return {
            "present": False,
            "file_count": 0,
            "total_bytes": 0,
            "manifest_sha256": None,
        }
    entries: list[str] = []
    total_bytes = 0
    for candidate in sorted(directory.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if candidate.is_symlink():
            raise SidecarPerformanceError(f"sidecar support payload contains a link: {candidate}")
        if not candidate.is_file():
            continue
        size = candidate.stat().st_size
        total_bytes += size
        entries.append(
            "\t".join(
                (
                    candidate.relative_to(binary.parent).as_posix(),
                    str(size),
                    sha256_file(candidate),
                )
            )
        )
    return {
        "present": True,
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "manifest_sha256": sha256_text("\n".join(entries) + "\n"),
    }


def _read_smoke(path: Path, *, command: list[str], label: str, ordinal: int) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SidecarPerformanceError(f"{label} sample {ordinal} did not produce valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise SidecarPerformanceError(f"{label} sample {ordinal} sidecar health was not ok")
    readiness = payload.get("readiness_ms")
    if not isinstance(readiness, int) or readiness <= 0:
        raise SidecarPerformanceError(f"{label} sample {ordinal} has invalid readiness_ms")
    build_id = str(payload.get("build_id") or "")
    fingerprint = str(payload.get("source_fingerprint") or "")
    version = str(payload.get("version") or "")
    if not build_id or build_id == "unavailable" or not fingerprint or not version:
        raise SidecarPerformanceError(f"{label} sample {ordinal} lacks embedded build identity")
    return {
        "sample": ordinal,
        "readiness_ms": readiness,
        "version": version,
        "build_id": build_id,
        "source_fingerprint": fingerprint,
        "workspace_state": str(payload.get("workspace_state") or ""),
        "command": subprocess.list2cmdline(command),
    }


def run_samples(binary: Path, *, label: str, count: int, temporary_root: Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    smoke_script = ROOT / "scripts" / "smoke-sidecar.ps1"
    for ordinal in range(1, count + 1):
        output = temporary_root / f"{label}-{ordinal}.json"
        command = [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(smoke_script),
            "-Binary",
            str(binary),
            "-Output",
            str(output),
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise SidecarPerformanceError(
                f"{label} sample {ordinal} exited {completed.returncode}: "
                f"{(completed.stderr or completed.stdout)[-600:]}"
            )
        samples.append(_read_smoke(output, command=command, label=label, ordinal=ordinal))
    return samples


def median_ms(samples: list[dict[str, Any]]) -> int:
    values = [int(sample["readiness_ms"]) for sample in samples]
    if len(values) != MEDIAN_SAMPLE_COUNT:
        raise SidecarPerformanceError(f"expected {MEDIAN_SAMPLE_COUNT} median samples, got {len(values)}")
    return int(statistics.median(values))


def nearest_rank_p95_ms(samples: list[dict[str, Any]]) -> int:
    values = sorted(int(sample["readiness_ms"]) for sample in samples)
    if len(values) != P95_SAMPLE_COUNT:
        raise SidecarPerformanceError(f"expected {P95_SAMPLE_COUNT} P95 samples, got {len(values)}")
    return values[math.ceil(0.95 * len(values)) - 1]


def consistent_identity(samples: list[dict[str, Any]], *, label: str) -> dict[str, str]:
    if not samples:
        raise SidecarPerformanceError(f"{label} has no samples")
    keys = ("version", "build_id", "source_fingerprint", "workspace_state")
    identity: dict[str, str] = {}
    for key in keys:
        values = {str(sample.get(key) or "") for sample in samples}
        if len(values) != 1 or not next(iter(values)):
            raise SidecarPerformanceError(f"{label} samples have inconsistent {key}")
        identity[key] = next(iter(values))
    return identity


def evaluate(
    *,
    candidate_median_samples: list[dict[str, Any]],
    candidate_p95_samples: list[dict[str, Any]],
    baseline_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_identity = consistent_identity(
        [*candidate_median_samples, *candidate_p95_samples], label="candidate"
    )
    baseline_identity = consistent_identity(baseline_samples, label="baseline")
    candidate_median = median_ms(candidate_median_samples)
    candidate_p95 = nearest_rank_p95_ms(candidate_p95_samples)
    baseline_median = median_ms(baseline_samples)
    regression_ratio = round(candidate_median / baseline_median - 1.0, 6)
    checks = {
        "candidate_median": candidate_median <= MEDIAN_LIMIT_MS,
        "candidate_p95": candidate_p95 <= P95_LIMIT_MS,
        "baseline_identity_distinct": candidate_identity["build_id"] != baseline_identity["build_id"],
        "regression": regression_ratio <= MAX_REGRESSION_RATIO,
    }
    return {
        "candidate_identity": candidate_identity,
        "baseline_identity": baseline_identity,
        "candidate_median_ms": candidate_median,
        "candidate_p95_ms": candidate_p95,
        "baseline_median_ms": baseline_median,
        "regression_ratio": regression_ratio,
        "checks": checks,
        "status": "PASS" if all(checks.values()) else "FAIL",
    }


def report(
    *,
    candidate: Path,
    baseline: Path,
    candidate_median_samples: list[dict[str, Any]],
    candidate_p95_samples: list[dict[str, Any]],
    baseline_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_support = support_payload(candidate)
    if not candidate_support["present"] or int(candidate_support["file_count"]) <= 0:
        raise SidecarPerformanceError(
            "candidate must be an onedir sidecar with a non-empty adjacent _internal payload"
        )
    decision = evaluate(
        candidate_median_samples=candidate_median_samples,
        candidate_p95_samples=candidate_p95_samples,
        baseline_samples=baseline_samples,
    )
    return {
        "schema_version": 1,
        "report_type": "v14_sidecar_performance",
        "target_version": TARGET_VERSION,
        "actual_run": True,
        "recorded_at": utc_now(),
        "candidate": {
            "binary": repository_relative(candidate),
            "binary_sha256": sha256_file(candidate),
            "support_payload": candidate_support,
        },
        "baseline": {
            "binary": repository_relative(baseline),
            "binary_sha256": sha256_file(baseline),
            "support_payload": support_payload(baseline),
        },
        "candidate_median_samples": candidate_median_samples,
        "candidate_p95_samples": candidate_p95_samples,
        "baseline_samples": baseline_samples,
        "thresholds": {
            "candidate_median_ms": MEDIAN_LIMIT_MS,
            "candidate_p95_ms": P95_LIMIT_MS,
            "max_regression_ratio": MAX_REGRESSION_RATIO,
        },
        **decision,
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the v14 sidecar cold-start performance contract.")
    parser.add_argument("--candidate", required=True, help="Current candidate onedir sidecar under this repository.")
    parser.add_argument("--baseline", required=True, help="Verified prior-release sidecar under this repository.")
    parser.add_argument("--output", required=True, help="JSON path under build/v1400-evidence.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv or sys.argv[1:])
    output = controlled_output(arguments.output)
    candidate = controlled_binary(arguments.candidate)
    baseline = controlled_binary(arguments.baseline)
    payload: dict[str, Any]
    try:
        if sha256_file(candidate) == sha256_file(baseline):
            raise SidecarPerformanceError("candidate and baseline binaries must be distinct")
        with tempfile.TemporaryDirectory(prefix="v14-sidecar-performance-", dir=ROOT / "build") as raw:
            temporary_root = Path(raw)
            candidate_median_samples = run_samples(
                candidate, label="candidate-median", count=MEDIAN_SAMPLE_COUNT, temporary_root=temporary_root
            )
            candidate_p95_samples = run_samples(
                candidate, label="candidate-p95", count=P95_SAMPLE_COUNT, temporary_root=temporary_root
            )
            baseline_samples = run_samples(
                baseline, label="baseline", count=BASELINE_SAMPLE_COUNT, temporary_root=temporary_root
            )
            payload = report(
                candidate=candidate,
                baseline=baseline,
                candidate_median_samples=candidate_median_samples,
                candidate_p95_samples=candidate_p95_samples,
                baseline_samples=baseline_samples,
            )
    except SidecarPerformanceError as exc:
        payload = {
            "schema_version": 1,
            "report_type": "v14_sidecar_performance",
            "target_version": TARGET_VERSION,
            "actual_run": True,
            "recorded_at": utc_now(),
            "status": "FAIL",
            "error": str(exc),
        }
    write_report(output, payload)
    print(json.dumps({"status": payload["status"], "report": str(output)}, ensure_ascii=False))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
