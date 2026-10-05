"""Explicit frozen-sidecar measurements, retaining every actual observation.

This component-only collector never grants the desktop startup requirement.
Desktop acceptance additionally needs the production render-ready bridge; a
missing bridge is a failure, never a replacement with backend health timing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid

from rc_gate import ROOT, VERSION, COMPONENT_OBSERVERS, attachment, module, performance_comparison, requirement_id
from rc_test_evidence import controlled_environment


def python_cli_identity() -> dict:
    """Record script argv honestly; commit native argv without publishing it.

    Python's venv launcher may rewrite native argv[0]. The script argv is a
    separate explicitly typed observation, never a fabricated native command.
    Only the direct fixed script entry point may emit this protocol.
    """
    script = "scripts/record-rc-performance.py"
    original = getattr(sys, "orig_argv", None)
    runtime = Path(getattr(sys, "_base_executable", ""))
    if (not isinstance(original, list) or len(original) < 2
            or not sys.argv or sys.argv[0] != script or original[1:] != sys.argv):
        raise ValueError("the direct fixed Python script invocation is required")
    if (not runtime.is_absolute() or not isinstance(original[0], str)
            or Path(original[0]).resolve() != runtime.resolve()
            or sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12)):
        raise ValueError("the observed runtime is not the bound CPython 3.12 interpreter")
    if Path.cwd().resolve() != ROOT.resolve():
        raise ValueError("the actual working directory must be the repository root")

    def executable_hash(path: Path) -> str:
        result = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(block)
        return result.hexdigest()

    return {
        "command_protocol": "python-script-argv-v1", "command": list(sys.argv),
        "process_argv_sha256": hashlib.sha256(json.dumps(
            original, ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
        "interpreter": {
            "implementation": sys.implementation.name, "version": list(sys.version_info[:3]),
            "launcher_sha256": executable_hash(Path(sys.executable)),
            "runtime_sha256": executable_hash(runtime),
        },
    }


def reference(path: Path) -> dict[str, str]:
    relative = path.relative_to(ROOT).as_posix()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": relative, "sha256": digest.hexdigest()}


def write_once(path: Path, payload: dict) -> dict[str, str]:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return reference(path)


def binary_reference(value: str) -> dict[str, str]:
    path = ROOT / value
    if Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("binary must be repository-relative")
    ref = reference(path)
    resolved = attachment(ROOT, ref, binary=True)
    if resolved.suffix.lower() != ".exe" or resolved.stat().st_size < 1024 * 1024:
        raise ValueError("a frozen executable is required")
    return ref


def _embedded_source(binary: Path, observation: dict) -> dict:
    # An onedir archive has a separate native/Python payload. A manifest is
    # accepted here only when the actually launched /health identity agrees.
    manifest_path = binary.parent / "_internal/build-info.json"
    manifest = attachment(ROOT, reference(manifest_path))
    for field, observed in (("product_version", "version"), ("build_id", "build_id"),
                            ("source_fingerprint", "source_fingerprint"), ("workspace_state", "workspace_state")):
        if manifest.get(field) != observation.get(observed):
            raise ValueError("sidecar health differs from the packaged manifest")
    if observation.get("build_embedded") is not True or observation.get("component_build_id") != "sidecar-" + str(manifest.get("build_id")):
        raise ValueError("launched sidecar did not report an embedded component identity")
    return {"source_version": manifest["product_version"], "source_commit": manifest["git_commit"],
            "workspace_clean": manifest["workspace_state"] == "CLEAN",
            "source_tree_fingerprint": manifest["source_fingerprint"]}


def collect_sidecar_series(binary: dict[str, str], *, directory: Path, label: str, host: str, cache_state: str) -> dict:
    series_id = str(uuid.uuid4())
    series = {"run_id": series_id, "measurement_object": "sidecar", "measurement_protocol": "sidecar-health-v1",
              "startup_path": "frozen-sidecar", "cache_state": cache_state, "host_fingerprint": host,
              "binary": binary, "samples_ms": [], "sample_reports": []}
    for index in range(5):
        output = directory / f"{label}-{index}-observation.json"
        command = ["powershell", "-NoProfile", "-File", "scripts/smoke-sidecar.ps1", "-Binary", binary["path"],
                   "-Output", output.relative_to(ROOT).as_posix()]
        result = subprocess.run(command, cwd=ROOT, env=controlled_environment(dict(os.environ)), check=False)
        if result.returncode != 0:
            raise ValueError("the explicit sidecar smoke process failed")
        observation_reference = reference(output)
        observation = attachment(ROOT, observation_reference)
        milliseconds = observation.get("readiness_ms")
        if type(milliseconds) is not int or milliseconds <= 0 or observation.get("status") != "ok":
            raise ValueError("sidecar readiness observation is invalid")
        source = _embedded_source(ROOT / binary["path"], observation)
        if index and series["build_id"] != observation.get("build_id"):
            raise ValueError("the frozen component changed between starts")
        series["build_id"] = observation["build_id"]
        series["samples_ms"].append(milliseconds)
        series["sample_reports"].append(write_once(directory / f"{label}-{index}-sample.json", {
            "schema_version": 1, "report_type": "rc_startup_sample", "actual_run": True, "status": "PASS",
            "source": source, "source_after": source, "run_id": str(uuid.uuid4()), "series_run_id": series_id,
            "sample_index": index, "measurement_object": "sidecar", "measurement_protocol": "sidecar-health-v1",
            "startup_path": "frozen-sidecar", "cache_state": cache_state, "host_fingerprint": host,
            "build_id": observation["build_id"], "binary_sha256": binary["sha256"], "command": command,
            "cwd": ".", "exit_code": result.returncode, "timed_out": False, "observation": observation_reference,
        }))
        if reference(ROOT / binary["path"]) != binary:
            raise ValueError("binary changed during the five-sample collection")
    return series


def collect_desktop_series(binary: dict[str, str], *, directory: Path, label: str, cache_state: str, startup_path: str) -> dict:
    executable = ROOT / binary["path"]
    sidecar = binary_reference((executable.parent / "agent-backend.exe").relative_to(ROOT).as_posix())
    binaries = {"desktop": binary, "sidecar": sidecar}
    series_id = str(uuid.uuid4())
    series = {"run_id": series_id, "measurement_object": "desktop", "measurement_protocol": "desktop-render-ready-v1",
              "startup_path": startup_path, "cache_state": cache_state, "binaries": binaries,
              "samples_ms": [], "sample_reports": []}
    # A recorded warm-up precedes five independent measured process launches.
    # Fresh test-owned app data is used in every launch; OS caches are not reset.
    for ordinal in range(6):
        output = directory / f"{label}-{ordinal}-desktop-observation.json"
        # This is the real argv, not a redacted copy. Bind the interpreter with
        # executable= while keeping personal installation paths out of proof.
        command = [Path(sys.executable).name, "scripts/record-rc-desktop-startup.py", "--desktop", binary["path"], "--sidecar", sidecar["path"],
                   "--output", output.relative_to(ROOT).as_posix(), "--cache-state", cache_state, "--startup-path", startup_path]
        result = subprocess.run(command, executable=sys.executable, cwd=ROOT, env=controlled_environment(dict(os.environ)), check=False)
        if result.returncode != 0:
            raise ValueError("the desktop render-ready collector failed; a legacy app without the bridge is not a baseline")
        observation_reference = reference(output)
        observation = attachment(ROOT, observation_reference)
        milliseconds = observation.get("readiness_ms")
        if type(milliseconds) is not int or milliseconds <= 0 or observation.get("status") != "PASS":
            raise ValueError("actual desktop observation is invalid")
        if ordinal == 0:
            series["warmup_report"] = observation_reference
            series["build_id"] = observation["build_id"]
            series["host_fingerprint"] = observation["host_fingerprint"]
        elif series["build_id"] != observation["build_id"] or series["host_fingerprint"] != observation["host_fingerprint"]:
            raise ValueError("desktop/host identity changed between measured process launches")
        if ordinal:
            index = ordinal - 1
            series["samples_ms"].append(milliseconds)
            series["sample_reports"].append(write_once(directory / f"{label}-{index}-desktop-sample.json", {
                "schema_version": 1, "report_type": "rc_startup_sample", "actual_run": True, "status": "PASS",
                "source": observation["source"], "source_after": observation["source_after"], "run_id": str(uuid.uuid4()),
                "series_run_id": series_id, "sample_index": index, "measurement_object": "desktop", "measurement_protocol": "desktop-render-ready-v1",
                "startup_path": startup_path, "cache_state": cache_state, "host_fingerprint": observation["host_fingerprint"],
                "build_id": observation["build_id"], "binary_sha256": observation["binary_sha256"], "command": command,
                "cwd": ".", "exit_code": result.returncode, "timed_out": False, "observation": observation_reference,
            }))
        if label == "candidate" and ordinal == 1:
            manifests, observations = {}, {}
            for component, protocol in COMPONENT_OBSERVERS.items():
                manifests[component] = write_once(directory / f"{component}-manifest.json", observation["application_receipt"]["components"][component])
                observations[component] = write_once(directory / f"{component}-component-observation.json", {
                    "schema_version": 1, "report_type": "rc_component_observation", "target_version": VERSION, "kind": "automated",
                    "actual_run": True, "status": "PASS", "source": observation["source"], "source_after": observation["source_after"],
                    "component": component, "observer_protocol": protocol, "build_id": observation["build_id"],
                    "binary_sha256": observation["binary_sha256"], "command": command, "cwd": ".", "exit_code": 0, "timed_out": False,
                    "observation": observation_reference,
                })
            series["component_manifests"] = manifests
            series["component_observations"] = observations
            series["sidecar_payload"] = observation["sidecar_payload"]
    return series


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measurement-object", choices=("sidecar", "desktop"), required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--startup-path", required=True)
    parser.add_argument("--cache-state", choices=("warm", "cold"), required=True)
    args = parser.parse_args(argv)
    if ((args.measurement_object == "sidecar" and args.startup_path != "frozen-sidecar")
            or (args.measurement_object == "desktop" and args.startup_path not in {"installed-nsis", "installed-msi", "portable-desktop"})
            or args.cache_state != "warm"):
        parser.error("supported path/object with repeated warm process starts is required; no controlled cold cache is implemented")
    path = Path(args.output)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".json":
        parser.error("output must be a fresh repository-relative JSON")
    output = ROOT / path
    boundary = ROOT / "build/v1600-evidence"
    directory = output.with_suffix(".samples")
    if not output.resolve().is_relative_to(boundary.resolve()) or output.exists() or directory.exists():
        parser.error("output and sample directory must be new paths below build/v1600-evidence")
    # Verify existing ancestors before creating test-owned output directories.
    for parent in output.parents:
        if parent == ROOT:
            break
        if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400):
            parser.error("output parent is a reparse point")
    try:
        invocation = None
        if args.measurement_object == "desktop":
            if argv is not None:
                raise ValueError("the direct CLI, not an in-process synthetic argv, is required")
            invocation = python_cli_identity()
        baseline = binary_reference(args.baseline)
        candidate = binary_reference(args.candidate)
        if baseline["sha256"] == candidate["sha256"]:
            raise ValueError("a distinct frozen baseline is required")
        directory.mkdir(parents=True, exist_ok=False)
        source = module("generate_build_info")._release_source_identity(ROOT)
        host = hashlib.sha256((platform.node() + "\0" + platform.platform() + "\0" + platform.machine()).encode()).hexdigest()
        if args.measurement_object == "sidecar":
            report = {"schema_version": 1, "report_type": "rc_component_performance_comparison", "actual_run": True,
                      "scope": "sidecar_only", "desktop_startup_qualified": False, "source": source,
                      "baseline": collect_sidecar_series(baseline, directory=directory, label="baseline", host=host, cache_state=args.cache_state),
                      "candidate": collect_sidecar_series(candidate, directory=directory, label="candidate", host=host, cache_state=args.cache_state)}
        else:
            comparison = {"output": args.output,
                          "baseline": collect_desktop_series(baseline, directory=directory, label="baseline", cache_state=args.cache_state, startup_path=args.startup_path),
                          "candidate": collect_desktop_series(candidate, directory=directory, label="candidate", cache_state=args.cache_state, startup_path=args.startup_path)}
            performance_comparison(comparison, comparison["candidate"]["build_id"])
            report = {"schema_version": 1, "report_type": "rc_check_evidence", "target_version": VERSION, "kind": "automated",
                      "scope": "desktop_render_startup", "desktop_startup_qualified": True, "actual_run": True,
                      "source": source, "performance_comparison": comparison,
                      **invocation,
                      "cwd": ".", "exit_code": 0, "timed_out": False,
                      "checks": {requirement_id("automated", "startup_performance_comparison"): True}}
            if python_cli_identity() != invocation:
                raise ValueError("the actual interpreter or invocation changed during measurement")
        report["source_after"] = module("generate_build_info")._release_source_identity(ROOT)
        report["status"] = ("PASS" if args.measurement_object == "desktop" else "COMPONENT_MEASURED") if source == report["source_after"] else "FAIL"
        write_once(output, report)
        print(json.dumps({"status": report["status"], "scope": report["scope"], "desktop_startup_qualified": report["desktop_startup_qualified"]}))
        return 0 if report["status"] in {"PASS", "COMPONENT_MEASURED"} else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc), "desktop_startup_qualified": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
