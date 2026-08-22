from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_desktop_sidecar_uses_onedir_payload_and_bundles_internal_directory() -> None:
    runtime_build = (ROOT / "scripts" / "build-runtime.ps1").read_text(encoding="utf-8")
    desktop_build = (ROOT / "scripts" / "build-desktop.ps1").read_text(encoding="utf-8")
    candidate_smoke = (ROOT / "scripts" / "v14-candidate-runtime-smoke.ps1").read_text(encoding="utf-8")
    v14_performance = (ROOT / "scripts" / "v14-sidecar-performance.py").read_text(encoding="utf-8")
    config = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))

    for script in (runtime_build, desktop_build):
        assert "--onedir --name agent-backend" in script
        assert "--onefile --name agent-backend" not in script
        assert "Sync-SidecarSupportDirectory" in script

    assert config["bundle"]["externalBin"] == ["binaries/agent-backend"]
    assert config["bundle"]["resources"]["binaries/_internal/"] == "_internal/"
    assert "Snapshot-CandidateSidecarPayload" in candidate_smoke
    assert "Restore-SupportDirectory" in candidate_smoke
    assert "v14-sidecar-performance.py" in candidate_smoke
    assert "P95_SAMPLE_COUNT = 10" in v14_performance
    assert "MAX_REGRESSION_RATIO = 0.10" in v14_performance


def test_desktop_build_restores_the_tracked_onedir_placeholder_after_success_or_failure() -> None:
    desktop_build = (ROOT / "scripts" / "build-desktop.ps1").read_text(encoding="utf-8")

    cleanup_start = desktop_build.index("$sidecarSupportMayHaveChanged = $false")
    sync_mark = desktop_build.index("$sidecarSupportMayHaveChanged = $true", cleanup_start)
    sync = desktop_build.index("Sync-SidecarSupportDirectory", sync_mark)
    cleanup = desktop_build.rindex("} finally {")

    assert cleanup_start < sync_mark < sync < cleanup
    assert "function Read-HeadBlobBytes" in desktop_build
    assert "function Restore-TrackedSidecarSupportPlaceholder" in desktop_build
    assert "$startInfo.RedirectStandardOutput = $true" in desktop_build
    assert "Tracked placeholder must remain a single byte in HEAD" in desktop_build
    assert "[System.IO.File]::WriteAllBytes($placeholder, $headBytes)" in desktop_build
    assert "if ($sidecarSupportMayHaveChanged)" in desktop_build[cleanup:]
    assert "Restore-TrackedSidecarSupportPlaceholder" in desktop_build[cleanup:]
