from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SMOKE_SCRIPT = ROOT / "scripts" / "smoke-sidecar.ps1"
CANDIDATE = ROOT / "build" / "v1400-evidence" / "candidate-optimized" / "sidecar" / "agent-backend.exe"
BASELINE = ROOT / "build" / "v1400-evidence" / "pre-v14-runtime-backup" / "agent-backend-x86_64-pc-windows-msvc.exe"
CANDIDATE_MANIFEST = CANDIDATE.parent / "_internal" / "build-info.json"


def _run_smoke(binary: Path, output: Path, *, inherited_manifest: Path) -> dict[str, object]:
    environment = os.environ.copy()
    # Reproduce the exact build-runtime parent environment that used to relabel
    # the pre-v14 onefile sidecar as the current onedir candidate.
    environment["SIYI_BUILD_MANIFEST"] = str(inherited_manifest)
    environment["SIYI_BUILD_INFO_LOCKED"] = "1"
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SMOKE_SCRIPT),
            "-Binary",
            str(binary),
            "-Output",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, (completed.stderr or completed.stdout)[-2_000:]
    return json.loads(output.read_text(encoding="utf-8-sig"))


def test_packaged_smoke_isolates_inherited_build_manifest_for_candidate_and_baseline(
    tmp_path: Path,
) -> None:
    """Exercise the real PowerShell -> frozen-sidecar process boundary when available."""

    if os.name != "nt" or shutil.which("powershell.exe") is None:
        pytest.skip("requires Windows PowerShell")
    if not (CANDIDATE.is_file() and BASELINE.is_file() and CANDIDATE_MANIFEST.is_file()):
        pytest.skip("requires retained v14 candidate and pre-v14 baseline sidecars")

    expected_candidate = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    candidate = _run_smoke(
        CANDIDATE,
        tmp_path / "candidate.json",
        inherited_manifest=CANDIDATE_MANIFEST,
    )
    baseline = _run_smoke(
        BASELINE,
        tmp_path / "baseline.json",
        inherited_manifest=CANDIDATE_MANIFEST,
    )

    assert candidate["build_embedded"] is True
    assert candidate["build_id"] == expected_candidate["build_id"]
    assert candidate["source_fingerprint"] == expected_candidate["source_fingerprint"]
    assert baseline["build_embedded"] is True
    assert baseline["build_id"] != candidate["build_id"]
    assert baseline["source_fingerprint"] != candidate["source_fingerprint"]
