from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check-release-evidence.py"
SPEC = importlib.util.spec_from_file_location("check_release_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text("15.0.0\n", encoding="ascii")
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "release-test@example.invalid"],
        ["git", "config", "user.name", "Release Test"],
        ["git", "add", "VERSION"],
        ["git", "commit", "-m", "release evidence fixture"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    evidence_root = root / "build" / "v1500-evidence"
    evidence_root.mkdir(parents=True)
    return root, evidence_root, head


def _write_evidence(root: Path, evidence_root: Path, head: str, kind: str) -> Path:
    suffix = "-setup.exe" if kind == "NSIS" else ".msi"
    name = f"司忆_15.0.0_x64{suffix}"
    bundle = root / "desktop" / "src-tauri" / "target" / "release" / "bundle" / kind.lower()
    bundle.mkdir(parents=True)
    candidate_path = bundle / name
    candidate_path.write_bytes(f"real-{kind}-candidate".encode() + b"0" * (1024 * 1024))
    common_results = {
        "status": "ok",
        "version": "15.0.0",
        "desktop_started": True,
        "sidecar_stopped": True,
        "previous_version_upgrade": True,
        "schema_migrated": True,
        "in_place_upgrade_preserved_data": True,
        "uninstall_preserved_data": True,
        "reinstall_recognized_data": True,
        "final_uninstall": True,
        "package_files_removed": True,
    }
    if kind == "NSIS":
        common_results["reinstall_started"] = True
    else:
        common_results |= {
            "install": True,
            "uninstall": True,
            "uninstall_preserved_models": True,
            "reinstall": True,
        }
    payload = {
        "schema_version": 1,
        "report_type": f"release_{kind.lower()}_installer_live_evidence",
        "target_version": "15.0.0",
        "status": "PASS",
        "actual_run": True,
        "source": {
            "source_version": "15.0.0",
            "source_commit": head,
            "source_tree_fingerprint": "A" * 64,
            "workspace_clean": True,
            "build_id": "a" * 24,
        },
        "run": {"installer_kind": kind, "isolated_test_data": True},
        "artifacts": {
            "candidate": {
                "name": name,
                "version": "15.0.0",
                "sha256": _sha256(candidate_path),
                "bytes": candidate_path.stat().st_size,
            },
            "previous": {
                "name": f"司忆_14.0.0_x64{suffix}",
                "version": "14.0.0",
                "sha256": "B" * 64,
            },
            "build_manifest": {
                "product_version": "15.0.0",
                "git_commit": head,
                "workspace_state": "CLEAN",
                "source_fingerprint": "A" * 64,
                "build_id": "a" * 24,
            },
            "previous_build_manifest": {"product_version": "14.0.0"},
        },
        "checks": {"lifecycle": {"passed": True}},
        "results": common_results,
    }
    path = evidence_root / f"{kind.lower()}-installer-smoke.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_release_evidence_requires_both_source_bound_installer_lifecycles(
    tmp_path: Path,
) -> None:
    root, evidence_root, head = _repository(tmp_path)
    _write_evidence(root, evidence_root, head, "NSIS")
    _write_evidence(root, evidence_root, head, "MSI")

    MODULE.validate_release_evidence(root, evidence_root)


@pytest.mark.parametrize("mutation", ("legacy_report", "wrong_head", "tampered_bundle"))
def test_release_evidence_fails_closed_on_unbound_or_stale_claims(
    tmp_path: Path, mutation: str
) -> None:
    root, evidence_root, head = _repository(tmp_path)
    nsis_path = _write_evidence(root, evidence_root, head, "NSIS")
    _write_evidence(root, evidence_root, head, "MSI")
    payload = json.loads(nsis_path.read_text(encoding="utf-8"))
    if mutation == "legacy_report":
        payload["report_type"] = "v14_nsis_installer_live_evidence"
        nsis_path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "wrong_head":
        payload["source"]["source_commit"] = "0" * 40
        nsis_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        candidate = payload["artifacts"]["candidate"]["name"]
        bundle = root / "desktop" / "src-tauri" / "target" / "release" / "bundle" / "nsis" / candidate
        bundle.write_bytes(b"tampered")

    with pytest.raises(MODULE.EvidenceError):
        MODULE.validate_release_evidence(root, evidence_root)


def test_release_workflow_uses_dynamic_previous_release_and_prepublication_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "Git tag $env:GITHUB_REF_NAME does not match VERSION=$version" in workflow
    assert "repos/$env:GITHUB_REPOSITORY/releases?per_page=100" in workflow
    assert "SIYI_PREVIOUS_NSIS" in workflow
    assert "SIYI_PREVIOUS_MSI" in workflow
    assert "-ReleaseEvidence" in workflow
    assert "check-release-evidence.py" in workflow
    assert "check-release-metadata.py --release-preflight" in workflow
    assert workflow.index("Validate release evidence before upload") < workflow.index(
        "Upload release artifacts"
    )
    publish = workflow.split("Publish tagged GitHub release", maxsplit=1)[1]
    assert publish.index("check-release-evidence.py") < publish.index("gh release create")
    assert "v7.0.0" not in workflow
    assert "Siyi_7.0.0" not in workflow


def test_release_scripts_cover_required_v15_gates_without_current_version_literals() -> None:
    test_script = (ROOT / "scripts" / "test.ps1").read_text(encoding="utf-8")
    build_script = (ROOT / "scripts" / "build-desktop.ps1").read_text(encoding="utf-8")
    nsis = (ROOT / "scripts" / "smoke-installer.ps1").read_text(encoding="utf-8")
    msi = (ROOT / "scripts" / "smoke-msi.ps1").read_text(encoding="utf-8")
    assert "--cov-fail-under=80" in test_script
    assert "npm run test:desktop" in test_script
    assert "cargo test --locked" in test_script
    assert "AGENT_DATA_ROOT" in test_script
    assert "AGENT_DESKTOP_DATA_DIRECTORY" in test_script
    assert "smoke-installer.ps1" in build_script
    assert "smoke-msi.ps1" in build_script
    assert "--release-preflight" in build_script
    assert "PreviousNsisInstaller" in build_script
    assert "PreviousMsiInstaller" in build_script
    for script in (nsis, msi):
        assert "SIYI_RELEASE_EVIDENCE_SOURCE_IDENTITY" in script
        assert "AllowCanonicalReleaseName" in script
        assert "|Siyi)" in script
        assert "-EvidenceVersion $version" in script
        assert "target_version = $version" in script
        assert "SIYI_V14_EVIDENCE_SOURCE_IDENTITY compatibility alias" in script
    active_nsis = nsis.replace("$version -ne '14.0.0'", "")
    active_msi = msi.replace("$version -ne '14.0.0'", "")
    assert "ExpectedVersion '14.0.0'" not in active_nsis
    assert "ExpectedVersion '14.0.0'" not in active_msi
