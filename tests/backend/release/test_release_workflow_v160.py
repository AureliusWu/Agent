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


def test_acceptance_artifact_must_be_same_repository_commit_success():
    command = next(value["run"] for value in steps() if value.get("name") == "Verify v16 RC acceptance artifact provenance")
    assert "$run.head_sha -ne $env:GITHUB_SHA" in command
    assert "$run.repository.full_name -ne $env:GITHUB_REPOSITORY" in command
    assert "$run.status -ne 'completed'" in command
    assert "$run.conclusion -ne 'success'" in command
    assert "-notmatch '^[1-9][0-9]*$'" in command


def test_acceptance_download_cannot_overwrite_repository_source():
    step = next(value for value in steps() if value.get("name") == "Download source-bound v16 RC acceptance evidence")
    assert step["with"]["path"] == "${{ env.SIYI_EVIDENCE_ROOT }}/rc-input"
    assert step["with"]["name"] == "Siyi-RC-Acceptance-v${{ env.SIYI_RELEASE_VERSION }}"
    assert "run-id" in step["with"]
