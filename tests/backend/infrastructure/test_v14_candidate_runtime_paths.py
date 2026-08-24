from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "v14-candidate-runtime-smoke.ps1"
EVIDENCE_ROOT = ROOT / "build" / "v1400-evidence"


pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="requires Windows PowerShell",
)


def run_script(
    *arguments: str,
    evidence_version: str = "14.0.0",
    validate_only: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-EvidenceVersion",
        evidence_version,
        *arguments,
    ]
    if validate_only:
        command.append("-ValidateOnly")
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def run_validate(*arguments: str) -> subprocess.CompletedProcess[str]:
    return run_script(*arguments, validate_only=True)


def payload(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.lstrip("\ufeff"))


def test_candidate_smoke_preflight_keeps_default_output_compatible() -> None:
    result = payload(run_validate())

    assert result["status"] == "VALID"
    assert result["output_mode"] == "default"
    assert str(result["candidate_root"]).endswith(r"candidate-optimized")
    assert str(result["performance_output"]).endswith(r"candidate-optimized\sidecar-performance.json")


def test_candidate_smoke_preflight_routes_run_id_and_explicit_relative_directory_without_writing() -> None:
    run_id = f"pytest-a22-{uuid.uuid4().hex}"
    by_run_id = payload(run_validate("-RunId", run_id))
    assert by_run_id["output_mode"] == "isolated"
    assert str(by_run_id["candidate_root"]).endswith(rf"candidate-runs\{run_id}")
    assert str(by_run_id["performance_output"]).endswith(rf"candidate-runs\{run_id}\sidecar-performance.json")
    assert not (EVIDENCE_ROOT / "candidate-runs" / run_id).exists()

    directory_name = f"candidate-runs\\pytest-a22-output-{uuid.uuid4().hex}"
    by_directory = payload(run_validate("-OutputDirectory", directory_name))
    assert by_directory["output_mode"] == "isolated"
    assert str(by_directory["candidate_root"]).endswith(directory_name)
    assert not (EVIDENCE_ROOT / Path(directory_name)).exists()


def test_candidate_smoke_preflight_routes_fresh_attested_output_without_writing() -> None:
    run_id = f"pytest-a22-{uuid.uuid4().hex}"
    raw_path = Path("raw") / f"pytest-a22-{uuid.uuid4().hex}.json"
    result = payload(run_validate("-RunId", run_id, "-AttestedOutput", str(raw_path)))

    assert result["output_mode"] == "isolated"
    assert str(result["candidate_root"]).endswith(rf"candidate-runs\{run_id}")
    assert str(result["performance_output"]).endswith(str(raw_path).replace("/", "\\"))
    assert str(result["attested_output"]).endswith(str(raw_path).replace("/", "\\"))
    assert not (EVIDENCE_ROOT / "candidate-runs" / run_id).exists()
    assert not (EVIDENCE_ROOT / raw_path).exists()


def test_candidate_smoke_preflight_allows_existing_attested_parent_but_rejects_unsafe_or_existing_target() -> None:
    run_id = f"pytest-a22-{uuid.uuid4().hex}"
    raw_parent = EVIDENCE_ROOT / "raw"
    raw_parent.mkdir(parents=True, exist_ok=True)
    existing_raw = raw_parent / f"pytest-a22-existing-{uuid.uuid4().hex}.json"
    existing_raw.write_text("{}", encoding="utf-8")
    try:
        existing = run_validate("-RunId", run_id, "-AttestedOutput", str(existing_raw.relative_to(EVIDENCE_ROOT)))
    finally:
        existing_raw.unlink()
    assert existing.returncode != 0
    assert "Refusing to overwrite an existing attested output" in (existing.stderr + existing.stdout)

    for unsafe_path in (" ", r"..\escape.json", "raw:escape.json", "raw/not-json.txt"):
        rejected = run_validate("-RunId", run_id, "-AttestedOutput", unsafe_path)
        assert rejected.returncode != 0
        assert "AttestedOutput" in (rejected.stderr + rejected.stdout)

    nested = run_validate(
        "-RunId",
        run_id,
        "-AttestedOutput",
        rf"candidate-runs\{run_id}\sidecar-performance.json",
    )
    assert nested.returncode != 0
    assert "outside the candidate output directory" in (nested.stderr + nested.stdout)


def test_candidate_smoke_preflight_rejects_escape_ambiguous_or_existing_output() -> None:
    escaped = run_validate("-OutputDirectory", r"..\escape")
    assert escaped.returncode != 0
    assert "unsafe path segment" in (escaped.stderr + escaped.stdout)

    normalized_collision = run_validate("-OutputDirectory", r"candidate-runs\a22.")
    assert normalized_collision.returncode != 0
    assert "unsafe path segment" in (normalized_collision.stderr + normalized_collision.stdout)

    invalid_run_id = run_validate("-RunId", "a22.")
    assert invalid_run_id.returncode != 0
    assert "RunId must contain" in (invalid_run_id.stderr + invalid_run_id.stdout)

    ambiguous = run_validate("-RunId", "a22-run", "-OutputDirectory", "candidate-runs/a22-run")
    assert ambiguous.returncode != 0
    assert "mutually exclusive" in (ambiguous.stderr + ambiguous.stdout)

    existing_name = f"pytest-a22-existing-{uuid.uuid4().hex}"
    existing = EVIDENCE_ROOT / existing_name
    existing.mkdir(parents=True)
    try:
        existing_output = run_validate("-OutputDirectory", existing_name)
    finally:
        existing.rmdir()
    assert existing_output.returncode != 0
    assert "Refusing to reuse" in (existing_output.stderr + existing_output.stdout)


def file_metadata(root: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (
            entry.relative_to(root).as_posix(),
            entry.stat().st_size,
            entry.stat().st_mtime_ns,
        )
        for entry in root.rglob("*")
        if entry.is_file()
    )


def test_candidate_smoke_isolated_path_setup_uses_supported_new_item_path_and_preserves_history() -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    compact_version = evidence_version.replace("-", "")
    temporary_evidence_root = ROOT / "build" / f"v{compact_version}-evidence"
    run_id = f"run-{uuid.uuid4().hex}"
    baseline = EVIDENCE_ROOT / "pre-v14-runtime-backup"
    historical_candidate = EVIDENCE_ROOT / "candidate-optimized"
    assert baseline.is_dir()
    assert historical_candidate.is_dir()
    baseline_before = file_metadata(baseline)
    candidate_before = file_metadata(historical_candidate)

    try:
        # This reaches real directory creation and then fails before the build
        # because the unique fake evidence version intentionally has no
        # verified prior-release baseline.  It therefore catches unsupported
        # New-Item parameter binding without starting a build or sidecar.
        result = run_script("-RunId", run_id, evidence_version=evidence_version)
        output = result.stderr + result.stdout

        assert result.returncode != 0
        assert "A required runtime artifact or its verified performance baseline is missing" in output
        assert "parameter cannot be found that matches parameter name 'LiteralPath'" not in output
        run_root = temporary_evidence_root / "candidate-runs" / run_id
        assert (run_root / "runtime-restore-backup").is_dir()
        assert not (run_root / "sidecar").exists()
        assert file_metadata(baseline) == baseline_before
        assert file_metadata(historical_candidate) == candidate_before
    finally:
        if temporary_evidence_root.exists():
            shutil.rmtree(temporary_evidence_root)


def test_candidate_smoke_does_not_replace_the_root_portable_runtime() -> None:
    candidate_smoke = SCRIPT.read_text(encoding="utf-8")
    runtime_build = (ROOT / "scripts" / "build-runtime.ps1").read_text(encoding="utf-8")

    assert "-SkipPortableRuntimeSync -CargoTargetDirectory $cargoTarget" in candidate_smoke
    assert "$runtimeApp =" not in candidate_smoke
    assert "$runtimeSidecar =" not in candidate_smoke
    assert "runtime-_internal" not in candidate_smoke
    assert "$performanceBaseline =" in candidate_smoke
    assert "$stagingSidecarBackup =" in candidate_smoke
    assert "--baseline $performanceBaseline" in candidate_smoke
    assert "Copy-Item -LiteralPath $stagingSidecarBackup -Destination $sidecar -Force" in candidate_smoke
    assert "[switch]$SkipPortableRuntimeSync" in runtime_build
    assert "if ($SkipPortableRuntimeSync)" in runtime_build
    assert "(Join-Path $root 'VERSION')" in runtime_build
