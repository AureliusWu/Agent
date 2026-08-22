from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "v14-evidence.py"
ARCHIVE_READER = SCRIPT.parent / "read-pyinstaller-build-info.py"
SPEC = importlib.util.spec_from_file_location("v14_installer_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ARCHIVE_READER_SPEC = importlib.util.spec_from_file_location("pyinstaller_build_info_reader", ARCHIVE_READER)
assert ARCHIVE_READER_SPEC and ARCHIVE_READER_SPEC.loader
ARCHIVE_READER_MODULE = importlib.util.module_from_spec(ARCHIVE_READER_SPEC)
ARCHIVE_READER_SPEC.loader.exec_module(ARCHIVE_READER_MODULE)

SOURCE = {
    "source_version": "14.0.0",
    "source_commit": "1" * 40,
    "source_tree_fingerprint": "2" * 64,
    "workspace_clean": True,
}


def passed_checks(case_id: str) -> dict[str, dict[str, bool]]:
    return {
        name: {"passed": True}
        for name in MODULE.ATTESTED_CASE_POLICIES[case_id]["required_checks"]
    }


def installer_run(kind: str, elevation: str) -> dict[str, object]:
    return {
        "run_id": "a" * 32,
        "started_at": "2026-08-12T00:00:00Z",
        "finished_at": "2026-08-12T00:01:00Z",
        "installer_kind": kind,
        "elevation": elevation,
        "isolated_test_data": True,
    }


def candidate(
    name: str, *, version: str = "14.0.0", digest: str = "A" * 64
) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "bytes": 2 * 1024 * 1024,
        "sha256": digest,
    }


def build_manifest() -> dict[str, object]:
    return {
        "product_version": "14.0.0",
        "git_commit": "1" * 40,
        "source_fingerprint": "2" * 64,
        "workspace_state": "CLEAN",
        "build_id": "3" * 24,
        "component_build_id": "sidecar-" + "3" * 24,
        "bytes": 4096,
        "sha256": "4" * 64,
    }


def previous_build_manifest() -> dict[str, object]:
    payload = build_manifest()
    payload["product_version"] = "13.0.0"
    payload["git_commit"] = "5" * 40
    payload["source_fingerprint"] = "6" * 64
    payload["build_id"] = "7" * 24
    payload["component_build_id"] = "sidecar-" + "7" * 24
    payload["sha256"] = "8" * 64
    return payload


def legacy_onefile_previous_identity() -> dict[str, object]:
    return {
        "identity_mode": "legacy_onefile_embedded_manifest",
        "product_version": "13.0.0",
        "git_commit": "5" * 40,
        "source_fingerprint": "6" * 64,
        "workspace_state": "CLEAN",
        "build_id": "7" * 24,
        "component_build_id": "sidecar-" + "7" * 24,
        "embedded_manifest_entry": "build-info.json",
        "embedded_manifest_bytes": 825,
        "embedded_manifest_sha256": "8" * 64,
        "installer_version": "13.0.0",
        "installer_sha256": "C" * 64,
        "executable_name": "agent-backend-x86_64-pc-windows-msvc.exe",
        "executable_bytes": 2 * 1024 * 1024,
        "executable_sha256": "9" * 64,
    }


def valid_nsis_payload() -> dict[str, object]:
    return {
        "source": dict(SOURCE),
        "checks": passed_checks("A27"),
        "run": installer_run("NSIS", "NON_ADMINISTRATOR"),
        "artifacts": {
            "candidate": candidate("司忆_14.0.0_x64-setup.exe"),
            "previous": candidate(
                "司忆_13.0.0_x64-setup.exe", version="13.0.0", digest="C" * 64
            ),
            "previous_build_manifest": previous_build_manifest(),
            "build_manifest": build_manifest(),
        },
        "results": {
            "status": "ok",
            "version": "14.0.0",
            "previous_version_upgrade": True,
            "schema_migrated": True,
            "migration_backup": "agent.db.v41.pre-v42.bak",
            "in_place_upgrade_preserved_data": True,
            "uninstall_preserved_data": True,
            "reinstall_started": True,
            "reinstall_recognized_data": True,
            "final_uninstall": True,
        },
    }


def write_log(root: Path, evidence_root: Path, name: str) -> dict[str, object]:
    path = evidence_root / name
    payload = f"real {name} log\n".encode()
    path.write_bytes(payload)
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest().upper(),
        "bytes": len(payload),
    }


def valid_msi_payload(root: Path, evidence_root: Path) -> dict[str, object]:
    observation_names = (
        "microphone_permission_grant",
        "microphone_permission_denial",
        "local_stt_transcription",
        "windows_tts_playback",
        "ollama_detection",
    )
    required_true = (
        "install",
        "desktop_started",
        "sidecar_stopped",
        "previous_version_upgrade",
        "schema_migrated",
        "in_place_upgrade_preserved_data",
        "uninstall",
        "uninstall_preserved_data",
        "uninstall_preserved_models",
        "reinstall",
        "reinstall_recognized_data",
        "final_uninstall",
        "package_files_removed",
    )
    return {
        "source": dict(SOURCE),
        "checks": passed_checks("A28"),
        "run": installer_run("MSI", "ADMINISTRATOR"),
        "artifacts": {
            "candidate": candidate("司忆_14.0.0_x64_zh-CN.msi"),
            "previous": candidate(
                "司忆_13.0.0_x64_zh-CN.msi", version="13.0.0", digest="C" * 64
            ),
            "previous_build_manifest": previous_build_manifest(),
            "build_manifest": build_manifest(),
        },
        "operator_acceptance": {
            "interactive": True,
            "operator": "release-operator",
            "all_confirmed": True,
            "observations": [
                {
                    "name": name,
                    "confirmed": True,
                    "confirmed_at": "2026-08-12T00:00:30Z",
                    "challenge_sha256": "B" * 64,
                }
                for name in observation_names
            ],
        },
        "results": {
            "status": "ok",
            "version": "14.0.0",
            "migration_backup": "agent.db.v41.pre-v42.bak",
            **{name: True for name in required_true},
        },
        "logs": [
            write_log(root, evidence_root, "msi-install-a.log"),
            write_log(root, evidence_root, "msi-upgrade-a.log"),
            write_log(root, evidence_root, "msi-uninstall-a.log"),
        ],
    }


def test_a27_requires_runner_attested_nsis_lifecycle() -> None:
    policy = MODULE.ATTESTED_CASE_POLICIES["A27"]
    assert policy["command_path"] == "scripts/smoke-installer.ps1"
    assert policy["raw_source"] is True
    payload = valid_nsis_payload()
    MODULE._validate_required_attested_checks(
        payload, case_id="A27", required_checks=policy["required_checks"]
    )
    MODULE._validate_a27_nsis_installer_live(payload, case_id="A27")


def test_a27_accepts_a_hash_bound_legacy_onefile_previous_sidecar() -> None:
    payload = valid_nsis_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    artifacts["previous_build_manifest"] = legacy_onefile_previous_identity()

    MODULE._validate_a27_nsis_installer_live(payload, case_id="A27")


@pytest.mark.parametrize(
    "mutation",
    ("missing_manifest_hash", "tampered_installer_hash", "wrong_manifest_state"),
)
def test_a27_rejects_unbound_or_tampered_legacy_onefile_manifest(mutation: str) -> None:
    payload = valid_nsis_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    legacy = legacy_onefile_previous_identity()
    if mutation == "missing_manifest_hash":
        legacy.pop("embedded_manifest_sha256")
    elif mutation == "tampered_installer_hash":
        legacy["installer_sha256"] = "D" * 64
    else:
        legacy["workspace_state"] = "DIRTY"
    artifacts["previous_build_manifest"] = legacy

    with pytest.raises(MODULE.EvidenceValidationError, match="legacy onefile"):
        MODULE._validate_a27_nsis_installer_live(payload, case_id="A27")


def test_a27_keeps_the_v14_candidate_manifest_requirement_strict() -> None:
    payload = valid_nsis_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    artifacts["build_manifest"] = legacy_onefile_previous_identity()

    with pytest.raises(MODULE.EvidenceValidationError, match="artifacts.build_manifest"):
        MODULE._validate_a27_nsis_installer_live(payload, case_id="A27")


def test_archive_reader_extracts_actual_pre_v14_onefile_manifest_when_available() -> None:
    """Exercise the archived v13 onefile sidecar without executing it."""

    old_binary = (
        SCRIPT.parents[1]
        / "build"
        / "v1400-evidence"
        / "pre-v14-runtime-backup"
        / "agent-backend.exe"
    )
    if not old_binary.is_file():
        pytest.skip("archived pre-v14 onefile sidecar is unavailable in this checkout")

    completed = subprocess.run(
        [sys.executable, str(ARCHIVE_READER), str(old_binary)],
        cwd=SCRIPT.parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["archive_entry"] == "build-info.json"
    assert payload["product_version"] == "13.0.0"
    assert payload["workspace_state"] == "CLEAN"
    assert len(payload["git_commit"]) == 40
    assert len(payload["source_fingerprint"]) == 64
    assert payload["component_build_id"] == f"sidecar-{payload['build_id']}"
    assert payload["embedded_manifest_bytes"] > 0
    assert len(payload["embedded_manifest_sha256"]) == 64


def test_archive_reader_rejects_a_link_before_resolving_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    linked_binary = tmp_path / "sidecar.exe"
    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        return path == linked_binary or original_is_symlink(path)

    def fail_resolve(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("a symlink input must not be resolved")

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    monkeypatch.setattr(Path, "resolve", fail_resolve)

    with pytest.raises(ARCHIVE_READER_MODULE.ArchiveReadError, match="regular file"):
        ARCHIVE_READER_MODULE.extract_embedded_build_info(linked_binary)


def test_archive_reader_cli_rejects_non_pyinstaller_input_without_traceback(tmp_path: Path) -> None:
    invalid_binary = tmp_path / "not-a-pyinstaller.exe"
    invalid_binary.write_bytes(b"not a PyInstaller archive")

    completed = subprocess.run(
        [sys.executable, str(ARCHIVE_READER), str(invalid_binary)],
        cwd=SCRIPT.parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Could not safely read the embedded PyInstaller build manifest." in completed.stderr
    assert "Traceback" not in completed.stderr


def test_a27_rejects_admin_or_missing_previous_upgrade() -> None:
    payload = valid_nsis_payload()
    payload["run"]["elevation"] = "ADMINISTRATOR"  # type: ignore[index]
    with pytest.raises(MODULE.EvidenceValidationError, match="required elevation"):
        MODULE._validate_a27_nsis_installer_live(payload, case_id="A27")


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("missing_previous", "artifacts.previous"),
        ("same_digest", "distinct, older"),
        ("not_older", "distinct, older"),
        ("previous_manifest_version", "previous_build_manifest"),
        ("candidate_source", "runner-bound source"),
    ),
)
def test_a27_rejects_unbound_upgrade_artifacts(mutation: str, message: str) -> None:
    payload = valid_nsis_payload()
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    if mutation == "missing_previous":
        artifacts.pop("previous")
    elif mutation == "same_digest":
        artifacts["previous"]["sha256"] = artifacts["candidate"]["sha256"]  # type: ignore[index]
    elif mutation == "not_older":
        artifacts["previous"]["version"] = "14.0.0"  # type: ignore[index]
        artifacts["previous"]["name"] = "司忆_14.0.0_x64-setup.exe"  # type: ignore[index]
    elif mutation == "previous_manifest_version":
        artifacts["previous_build_manifest"]["product_version"] = "12.0.0"  # type: ignore[index]
    else:
        artifacts["build_manifest"]["git_commit"] = "9" * 40  # type: ignore[index]
    with pytest.raises(MODULE.EvidenceValidationError, match=message):
        MODULE._validate_a27_nsis_installer_live(payload, case_id="A27")

    payload = valid_nsis_payload()
    payload["results"]["previous_version_upgrade"] = False  # type: ignore[index]
    with pytest.raises(MODULE.EvidenceValidationError, match="upgrade, migration"):
        MODULE._validate_a27_nsis_installer_live(payload, case_id="A27")


def test_a28_requires_admin_interactive_full_lifecycle_and_logs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    evidence_root = root / "build" / "v1400-evidence"
    evidence_root.mkdir(parents=True)
    payload = valid_msi_payload(root, evidence_root)
    policy = MODULE.ATTESTED_CASE_POLICIES["A28"]
    MODULE._validate_required_attested_checks(
        payload, case_id="A28", required_checks=policy["required_checks"]
    )
    MODULE._validate_a28_msi_installer_live(
        payload,
        case_id="A28",
        repository_root=root,
        evidence_root=evidence_root,
    )

    # Legacy onefile proof is intentionally an A27-only compatibility path;
    # it cannot relax the administrator MSI lifecycle contract.
    legacy_payload = valid_msi_payload(root, evidence_root)
    legacy_artifacts = legacy_payload["artifacts"]
    assert isinstance(legacy_artifacts, dict)
    legacy_artifacts["previous_build_manifest"] = legacy_onefile_previous_identity()
    with pytest.raises(MODULE.EvidenceValidationError, match="previous_build_manifest"):
        MODULE._validate_a28_msi_installer_live(
            legacy_payload,
            case_id="A28",
            repository_root=root,
            evidence_root=evidence_root,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("non_admin", "required elevation"),
        ("missing_voice", "observation set is incomplete"),
        ("tampered_log", "no longer matches"),
    ),
)
def test_a28_rejects_incomplete_or_tampered_claims(
    tmp_path: Path, mutation: str, message: str
) -> None:
    root = tmp_path / "repo"
    evidence_root = root / "build" / "v1400-evidence"
    evidence_root.mkdir(parents=True)
    payload = valid_msi_payload(root, evidence_root)
    if mutation == "non_admin":
        payload["run"]["elevation"] = "NON_ADMINISTRATOR"  # type: ignore[index]
    elif mutation == "missing_voice":
        payload["operator_acceptance"]["observations"].pop()  # type: ignore[index]
    else:
        (evidence_root / "msi-install-a.log").write_text("tampered", encoding="utf-8")
    with pytest.raises(MODULE.EvidenceValidationError, match=message):
        MODULE._validate_a28_msi_installer_live(
            payload,
            case_id="A28",
            repository_root=root,
            evidence_root=evidence_root,
        )


def test_installer_scripts_require_source_binding_and_immutable_raw_output() -> None:
    nsis = (SCRIPT.parents[0] / "smoke-installer.ps1").read_text(encoding="utf-8")
    msi = (SCRIPT.parents[0] / "smoke-msi.ps1").read_text(encoding="utf-8")
    for script in (nsis, msi):
        assert "SIYI_V14_EVIDENCE_SOURCE_IDENTITY" in script
        assert "[System.IO.FileMode]::CreateNew" in script
        assert "build\\v1400-evidence" in script
    assert "MSI desktop acceptance requires an interactive ConsoleHost" in msi
    assert "administrator_execution = $isAdministrator" in msi
    assert "candidate_build_identity_matches_source" in nsis
    assert "candidate_build_identity_matches_source" in msi
    assert "previous_build_identity_matches_artifact" in nsis
    assert "previous_build_identity_matches_artifact" in msi
    assert "A27 NSIS acceptance must run from a non-administrator process" in nsis
    assert "A28 MSI acceptance requires an administrator process" in msi
    assert nsis.index("A27 NSIS acceptance must run") < nsis.index("$install = Start-Process")
    assert "legacy_onefile_embedded_manifest" in nsis
    assert "read-pyinstaller-build-info.py" in nsis
    assert "-AllowLegacyOneFile $true" in nsis
    legacy_identity = nsis.split("$previousApplication =", maxsplit=1)[1].split(
        "New-Item -ItemType Directory", maxsplit=1
    )[0]
    assert "$previousSidecar = Get-ChildItem" in legacy_identity
    assert "Previous NSIS installation did not produce the backend sidecar." in legacy_identity
    assert "-Sidecar $previousSidecar" in legacy_identity
    assert "-Sidecar $previousApplication" not in legacy_identity
    candidate_identity = nsis.split("$candidateBuildIdentity =", maxsplit=1)[1]
    assert "-AllowLegacyOneFile $true" not in candidate_identity.split("} else { $null }", maxsplit=1)[0]
    assert msi.index("A28 MSI acceptance requires an administrator process") < msi.index(
        "Invoke-Msi @('/i'"
    )


@pytest.mark.parametrize("name", ("smoke-installer.ps1", "smoke-msi.ps1"))
def test_installer_script_has_valid_powershell_ast(name: str) -> None:
    shell = shutil.which("powershell") or shutil.which("pwsh")
    assert shell is not None, "Windows installer tests require PowerShell"
    script = SCRIPT.parents[0] / name
    command = (
        "$tokens=$null;$errors=$null;"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}',"
        "[ref]$tokens,[ref]$errors);"
        "if($errors.Count){$errors|ForEach-Object{Write-Error $_};exit 1}"
    )
    subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )
