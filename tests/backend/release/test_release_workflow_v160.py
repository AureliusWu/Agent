from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def steps():
    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]["package"]["steps"]


def test_total_rc_gate_runs_before_artifact_upload():
    entries = steps()
    gate = next(i for i, value in enumerate(entries) if value.get("name") == "Enforce total v16 RC gate before upload")
    upload = next(i for i, value in enumerate(entries) if value.get("name") == "Upload release artifacts")
    assert gate < upload
    assert "scripts/rc_gate.py --bundle" in entries[gate]["run"]
    assert "if ($LASTEXITCODE -ne 0)" in entries[gate]["run"]


def test_total_rc_gate_rechecked_before_remote_creation():
    command = next(value["run"] for value in steps() if value.get("name") == "Publish tagged GitHub release")
    assert command.index("scripts/rc_gate.py") < command.index("gh release create")
    assert "Total v16 RC gate failed immediately before publication" in command


def test_acceptance_artifact_provenance_is_checked_before_and_after_download():
    command = next(value["run"] for value in steps() if value.get("name") == "Verify v16 RC acceptance artifact provenance")
    assert "scripts/validate-rc-run.py" in command
    assert "--commit $env:GITHUB_SHA" in command
    assert "--repository $env:GITHUB_REPOSITORY" in command
    assert "--write-receipt $selectionReceipt" in command
    assert "-notmatch '^[1-9][0-9]*$'" in command
    after = next(value["run"] for value in steps() if value.get("name") == "Enforce total v16 RC gate before upload")
    assert "--expected-receipt $selectionReceipt" in after
    assert after.index("scripts/validate-rc-run.py") < after.index("scripts/import-accepted-rc.py")


def test_cross_run_rc_artifact_download_has_actions_read_permission():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"]["actions"] == "read"
    assert workflow["permissions"]["contents"] == "write"


def test_acceptance_download_cannot_overwrite_repository_source():
    step = next(value for value in steps() if value.get("name") == "Download source-bound v16 RC acceptance evidence")
    command = step["run"]
    assert "download-actions" in command and "--artifact-id $env:SIYI_RC_ARTIFACT_ID" in command
    assert "--sha256 $sha256 --size $selection.artifact.size_in_bytes" in command
    assert '--directory "$env:SIYI_EVIDENCE_ROOT/rc-input"' in command
    assert command.index("download-actions") < command.index(" receive --archive $archive")
    assert "if ($LASTEXITCODE -ne 0)" in command
    assert "uses" not in step


def test_v16_release_reads_the_fixed_accepted_metadata_snapshot_without_changing_v15():
    entries = steps()
    v16 = next(value["run"] for value in entries if value.get("name") == "Enforce total v16 RC gate before upload")
    legacy = next(value["run"] for value in entries if value.get("name") == "Validate release evidence before upload")
    assert "check-release-metadata.py --release --accepted-snapshot" in v16
    assert "--accepted-snapshot" not in legacy
    publish = next(value["run"] for value in entries if value.get("name") == "Publish tagged GitHub release")
    assert "$metadataArguments = @('--release')" in publish
    assert "if ($version.StartsWith('16.'))" in publish
    assert "$metadataArguments += '--accepted-snapshot'" in publish
    assert "check-release-metadata.py @metadataArguments" in publish
