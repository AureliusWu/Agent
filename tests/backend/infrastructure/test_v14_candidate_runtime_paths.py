from __future__ import annotations

import hashlib
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
        errors="strict",
        check=False,
    )


def run_validate(
    *arguments: str,
    evidence_version: str = "14.0.0",
) -> subprocess.CompletedProcess[str]:
    return run_script(
        *arguments,
        evidence_version=evidence_version,
        validate_only=True,
    )


def payload(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.lstrip("\ufeff"))


def test_candidate_smoke_preflight_keeps_default_output_compatible() -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    temporary_evidence_root = ROOT / "build" / f"v{evidence_version.replace('-', '')}-evidence"
    result = payload(run_validate(evidence_version=evidence_version))

    assert result["status"] == "VALID"
    assert result["output_mode"] == "default"
    assert Path(str(result["candidate_root"])) == temporary_evidence_root / "candidate-optimized"
    assert Path(str(result["performance_output"])) == (
        temporary_evidence_root / "candidate-optimized" / "sidecar-performance.json"
    )
    assert len(str(result["hash_probe_sha256"])) == 64
    assert all(character in "0123456789ABCDEF" for character in str(result["hash_probe_sha256"]))
    assert not temporary_evidence_root.exists()


@pytest.mark.parametrize("output_mode", ["default", "run_id", "directory", "attested"])
def test_candidate_smoke_preflight_preserves_utf8_from_codepage_936_without_writing(
    output_mode: str,
) -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    evidence_root = ROOT / "build" / f"v{evidence_version.replace('-', '')}-evidence"
    run_id = f"pytest-a22-{uuid.uuid4().hex}"
    options: dict[str, str] = {}
    expected_candidate = evidence_root / "candidate-optimized"
    expected_attested = None
    if output_mode == "run_id" or output_mode == "attested":
        options = {"RunId": run_id}
        expected_candidate = evidence_root / "candidate-runs" / run_id
    elif output_mode == "directory":
        # This guarantees non-ASCII JSON even on a checkout with an ASCII root.
        directory = f"candidate-runs\\司忆-中文-{uuid.uuid4().hex}"
        options = {"OutputDirectory": directory}
        expected_candidate = evidence_root / directory
    if output_mode == "attested":
        raw = f"raw\\司忆-中文-{uuid.uuid4().hex}.json"
        options["AttestedOutput"] = raw
        expected_attested = evidence_root / raw
    expected_performance = expected_attested or expected_candidate / "sidecar-performance.json"
    environment = os.environ.copy()
    environment.update(
        SIYI_TEST_PREFLIGHT_SCRIPT=str(SCRIPT),
        SIYI_TEST_PREFLIGHT_VERSION=evidence_version,
        SIYI_TEST_PREFLIGHT_OPTIONS=json.dumps(options),
    )
    completed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "$ErrorActionPreference='Stop'; "
            "[Console]::OutputEncoding=[Text.Encoding]::GetEncoding(936); "
            "$OutputEncoding=[Console]::OutputEncoding; "
            "if ([Console]::OutputEncoding.CodePage -ne 936) { throw '936 console was not selected' }; "
            "$preflightOptions=@{}; "
            "$preflightJson=ConvertFrom-Json $env:SIYI_TEST_PREFLIGHT_OPTIONS; "
            "foreach ($preflightProperty in $preflightJson.PSObject.Properties) { "
            "$preflightOptions[$preflightProperty.Name]=[string]$preflightProperty.Value }; "
            "& $env:SIYI_TEST_PREFLIGHT_SCRIPT -EvidenceVersion $env:SIYI_TEST_PREFLIGHT_VERSION "
            "@preflightOptions -ValidateOnly",
        ],
        cwd=ROOT, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="strict", check=False,
    )
    result = payload(completed)
    assert result["status"] == "VALID"
    assert result["output_mode"] == ("default" if output_mode == "default" else "isolated")
    assert Path(str(result["candidate_root"])) == expected_candidate
    assert Path(str(result["performance_output"])) == expected_performance
    assert result["attested_output"] == (str(expected_attested) if expected_attested else None)
    assert result["hash_probe_sha256"] == hashlib.sha256((ROOT / "VERSION").read_bytes()).hexdigest().upper()
    assert not evidence_root.exists()


def test_candidate_smoke_preflight_routes_run_id_and_explicit_relative_directory_without_writing() -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    compact_version = evidence_version.replace("-", "")
    temporary_evidence_root = ROOT / "build" / f"v{compact_version}-evidence"
    run_id = f"pytest-a22-{uuid.uuid4().hex}"
    by_run_id = payload(run_validate("-RunId", run_id, evidence_version=evidence_version))
    assert by_run_id["output_mode"] == "isolated"
    assert Path(str(by_run_id["candidate_root"])) == temporary_evidence_root / "candidate-runs" / run_id
    assert Path(str(by_run_id["performance_output"])) == (
        temporary_evidence_root / "candidate-runs" / run_id / "sidecar-performance.json"
    )
    assert not (temporary_evidence_root / "candidate-runs" / run_id).exists()

    directory_name = f"candidate-runs\\pytest-a22-output-{uuid.uuid4().hex}"
    by_directory = payload(
        run_validate("-OutputDirectory", directory_name, evidence_version=evidence_version)
    )
    assert by_directory["output_mode"] == "isolated"
    assert Path(str(by_directory["candidate_root"])) == temporary_evidence_root / Path(directory_name)
    assert not (temporary_evidence_root / Path(directory_name)).exists()
    assert not temporary_evidence_root.exists()


def test_candidate_smoke_preflight_routes_fresh_attested_output_without_writing() -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    compact_version = evidence_version.replace("-", "")
    temporary_evidence_root = ROOT / "build" / f"v{compact_version}-evidence"
    run_id = f"pytest-a22-{uuid.uuid4().hex}"
    raw_path = Path("raw") / f"pytest-a22-{uuid.uuid4().hex}.json"
    result = payload(
        run_validate(
            "-RunId",
            run_id,
            "-AttestedOutput",
            str(raw_path),
            evidence_version=evidence_version,
        )
    )

    assert result["output_mode"] == "isolated"
    assert Path(str(result["candidate_root"])) == temporary_evidence_root / "candidate-runs" / run_id
    assert Path(str(result["performance_output"])) == temporary_evidence_root / raw_path
    assert Path(str(result["attested_output"])) == temporary_evidence_root / raw_path
    assert not (temporary_evidence_root / "candidate-runs" / run_id).exists()
    assert not (temporary_evidence_root / raw_path).exists()
    assert not temporary_evidence_root.exists()

    root_raw_path = Path(f"pytest-a22-root-{uuid.uuid4().hex}.json")
    root_result = payload(
        run_validate(
            "-RunId",
            f"pytest-a22-{uuid.uuid4().hex}",
            "-AttestedOutput",
            str(root_raw_path),
            evidence_version=evidence_version,
        )
    )
    assert Path(str(root_result["performance_output"])) == temporary_evidence_root / root_raw_path
    assert Path(str(root_result["attested_output"])) == temporary_evidence_root / root_raw_path
    assert not temporary_evidence_root.exists()


def test_candidate_smoke_preflight_allows_existing_attested_parent_but_rejects_unsafe_or_existing_target() -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    temporary_evidence_root = ROOT / "build" / f"v{evidence_version.replace('-', '')}-evidence"
    run_id = f"pytest-a22-{uuid.uuid4().hex}"
    raw_parent = temporary_evidence_root / "raw"
    raw_parent.mkdir(parents=True, exist_ok=True)
    existing_raw = raw_parent / f"pytest-a22-existing-{uuid.uuid4().hex}.json"
    existing_raw.write_text("{}", encoding="utf-8")
    try:
        existing = run_validate(
            "-RunId",
            run_id,
            "-AttestedOutput",
            str(existing_raw.relative_to(temporary_evidence_root)),
            evidence_version=evidence_version,
        )
    finally:
        shutil.rmtree(temporary_evidence_root)
    assert existing.returncode != 0
    assert "Refusing to overwrite an existing attested output" in (existing.stderr + existing.stdout)

    for unsafe_path in (" ", r"..\escape.json", "raw:escape.json", "raw/not-json.txt"):
        rejected = run_validate(
            "-RunId",
            run_id,
            "-AttestedOutput",
            unsafe_path,
            evidence_version=evidence_version,
        )
        assert rejected.returncode != 0
        assert "AttestedOutput" in (rejected.stderr + rejected.stdout)

    nested = run_validate(
        "-RunId",
        run_id,
        "-AttestedOutput",
        rf"candidate-runs\{run_id}\sidecar-performance.json",
        evidence_version=evidence_version,
    )
    assert nested.returncode != 0
    assert "outside the candidate output directory" in (nested.stderr + nested.stdout)


def test_candidate_smoke_preflight_rejects_escape_ambiguous_or_existing_output() -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    temporary_evidence_root = ROOT / "build" / f"v{evidence_version.replace('-', '')}-evidence"
    escaped = run_validate(
        "-OutputDirectory",
        r"..\escape",
        evidence_version=evidence_version,
    )
    assert escaped.returncode != 0
    assert "unsafe path segment" in (escaped.stderr + escaped.stdout)

    normalized_collision = run_validate(
        "-OutputDirectory",
        r"candidate-runs\a22.",
        evidence_version=evidence_version,
    )
    assert normalized_collision.returncode != 0
    assert "unsafe path segment" in (normalized_collision.stderr + normalized_collision.stdout)

    invalid_run_id = run_validate(
        "-RunId",
        "a22.",
        evidence_version=evidence_version,
    )
    assert invalid_run_id.returncode != 0
    assert "RunId must contain" in (invalid_run_id.stderr + invalid_run_id.stdout)

    ambiguous = run_validate(
        "-RunId",
        "a22-run",
        "-OutputDirectory",
        "candidate-runs/a22-run",
        evidence_version=evidence_version,
    )
    assert ambiguous.returncode != 0
    assert "mutually exclusive" in (ambiguous.stderr + ambiguous.stdout)

    existing_name = f"pytest-a22-existing-{uuid.uuid4().hex}"
    existing = temporary_evidence_root / existing_name
    existing.mkdir(parents=True)
    try:
        existing_output = run_validate(
            "-OutputDirectory",
            existing_name,
            evidence_version=evidence_version,
        )
    finally:
        shutil.rmtree(temporary_evidence_root)
    assert existing_output.returncode != 0
    assert "Refusing to reuse" in (existing_output.stderr + existing_output.stdout)


def test_candidate_smoke_preflight_rejects_a_dangling_reparse_evidence_root(
    tmp_path: Path,
) -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    build_root = ROOT / "build"
    build_root_existed = build_root.is_dir()
    build_root.mkdir(exist_ok=True)
    evidence_root = build_root / f"v{evidence_version.replace('-', '')}-evidence"
    target = tmp_path / "junction-target"
    target.mkdir()
    environment = os.environ.copy()
    environment["SIYI_TEST_JUNCTION"] = str(evidence_root)
    environment["SIYI_TEST_JUNCTION_TARGET"] = str(target)
    created = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:SIYI_TEST_JUNCTION -Target $env:SIYI_TEST_JUNCTION_TARGET | Out-Null",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert created.returncode == 0, created.stderr or created.stdout
    target.rmdir()
    try:
        rejected = run_validate(evidence_version=evidence_version)
    finally:
        removed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$ErrorActionPreference='Stop'; Remove-Item -LiteralPath $env:SIYI_TEST_JUNCTION -Force",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert removed.returncode == 0, removed.stderr or removed.stdout
        if not build_root_existed:
            build_root.rmdir()
    assert rejected.returncode != 0
    assert "linked or non-directory candidate output" in (rejected.stderr + rejected.stdout)


def test_candidate_smoke_preflight_rejects_a_dangling_reparse_path_component(
    tmp_path: Path,
) -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    build_root = ROOT / "build"
    build_root_existed = build_root.is_dir()
    build_root.mkdir(exist_ok=True)
    evidence_root = build_root / f"v{evidence_version.replace('-', '')}-evidence"
    candidate_runs = evidence_root / "candidate-runs"
    candidate_runs.mkdir(parents=True)
    link = candidate_runs / "dangling"
    target = tmp_path / "junction-target"
    target.mkdir()
    environment = os.environ.copy()
    environment["SIYI_TEST_JUNCTION"] = str(link)
    environment["SIYI_TEST_JUNCTION_TARGET"] = str(target)
    created = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:SIYI_TEST_JUNCTION -Target $env:SIYI_TEST_JUNCTION_TARGET | Out-Null",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert created.returncode == 0, created.stderr or created.stdout
    target.rmdir()
    try:
        rejected = run_validate(
            "-OutputDirectory",
            r"candidate-runs\dangling\child",
            evidence_version=evidence_version,
        )
    finally:
        removed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$ErrorActionPreference='Stop'; Remove-Item -LiteralPath $env:SIYI_TEST_JUNCTION -Force",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert removed.returncode == 0, removed.stderr or removed.stdout
        candidate_runs.rmdir()
        evidence_root.rmdir()
        if not build_root_existed:
            build_root.rmdir()
    assert rejected.returncode != 0
    assert "linked or non-directory candidate output path component" in (
        rejected.stderr + rejected.stdout
    )


def test_candidate_smoke_preflight_rejects_a_dangling_attested_output(
    tmp_path: Path,
) -> None:
    evidence_version = f"pytest-a22-{uuid.uuid4().hex}"
    evidence_root = ROOT / "build" / f"v{evidence_version.replace('-', '')}-evidence"
    raw_parent = evidence_root / "raw"
    raw_parent.mkdir(parents=True)
    output_link = raw_parent / "result.json"
    target = tmp_path / "junction-target"
    target.mkdir()
    environment = os.environ.copy()
    environment["SIYI_TEST_JUNCTION"] = str(output_link)
    environment["SIYI_TEST_JUNCTION_TARGET"] = str(target)
    created = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:SIYI_TEST_JUNCTION -Target $env:SIYI_TEST_JUNCTION_TARGET | Out-Null",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert created.returncode == 0, created.stderr or created.stdout
    target.rmdir()
    try:
        rejected = run_validate(
            "-RunId",
            f"pytest-a22-{uuid.uuid4().hex}",
            "-AttestedOutput",
            r"raw\result.json",
            evidence_version=evidence_version,
        )
    finally:
        removed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$ErrorActionPreference='Stop'; Remove-Item -LiteralPath $env:SIYI_TEST_JUNCTION -Force",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert removed.returncode == 0, removed.stderr or removed.stdout
        raw_parent.rmdir()
        evidence_root.rmdir()
    assert rejected.returncode != 0
    assert "Refusing to overwrite an existing attested output" in (
        rejected.stderr + rejected.stdout
    )


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
    baseline_before = file_metadata(baseline) if baseline.is_dir() else None
    candidate_before = file_metadata(historical_candidate) if historical_candidate.is_dir() else None

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
        assert (file_metadata(baseline) if baseline.is_dir() else None) == baseline_before
        assert (
            file_metadata(historical_candidate) if historical_candidate.is_dir() else None
        ) == candidate_before
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
    assert "function Get-Sha256Hex" in candidate_smoke
    assert "Get-FileHash" not in candidate_smoke
    assert "[switch]$SkipPortableRuntimeSync" in runtime_build
    assert "if ($SkipPortableRuntimeSync)" in runtime_build
    assert "(Join-Path $root 'VERSION')" in runtime_build
