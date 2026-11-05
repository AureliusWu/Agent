"""Exercise dispatch authorization and non-release workflow boundaries only.

No installer, model, network, or GitHub mutation is executed by these tests.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/rc-desktop-candidate.yml"
READER_PACKAGES = {
    "pyinstaller": "6.21.0", "altgraph": "0.17.5", "packaging": "26.2",
    "pefile": "2024.8.26", "pyinstaller-hooks-contrib": "2026.6",
    "pywin32-ctypes": "0.2.3", "setuptools": "83.0.0",
}
ACTION_SHAS = {
    "actions/checkout": "d23441a48e516b6c34aea4fa41551a30e30af803",
    "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
    "actions/setup-node": "249970729cb0ef3589644e2896645e5dc5ba9c38",
    "actions/upload-artifact": "b7c566a772e6b6bfb58ed0dc250532a479d7789f",
}


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step(job, name):
    return next(item for item in job["steps"] if item.get("name") == name)


def test_only_manual_exact_source_dispatch_with_read_only_permissions():
    value = workflow()
    triggers = value.get("on", value.get(True))  # PyYAML 1.1 treats unquoted on as bool.
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"] == {
        "source_sha": {"description": "Exact current main SHA for this non-release desktop observation",
                       "required": True, "type": "string"}}
    assert value["permissions"] == {"contents": "read", "actions": "read"}
    assert value["concurrency"]["cancel-in-progress"] is False
    assert set(value["jobs"]) == {"build", "installed", "summarize"}
    assert all("permissions" not in job and "environment" not in job for job in value["jobs"].values())


def test_each_hosted_job_checks_identity_before_dependency_or_checkout_actions():
    guards = []
    for job in workflow()["jobs"].values():
        assert job["runs-on"] == "windows-2025"
        assert job["steps"][0]["name"] == "Require explicit own-main hosted dispatch"
        guard = job["steps"][0]
        assert guard["env"] == {"INPUT_SOURCE_SHA": "${{ inputs.source_sha }}"}
        assert "${{" not in guard["run"]
        guards.append(guard["run"])
        checkout = next(item for item in job["steps"] if str(item.get("uses", "")).startswith("actions/checkout@"))
        assert checkout["with"] == {"ref": "${{ github.sha }}", "fetch-depth": 0, "persist-credentials": False}
        newline = step(job, "Keep source bytes stable across fresh Windows jobs")
        assert newline["run"] == "git config --global core.autocrlf true"
        assert job["steps"].index(newline) < job["steps"].index(checkout)
    assert len(set(guards)) == 1


def test_every_action_uses_the_reviewed_immutable_v6_commit():
    observed = set()
    for job in workflow()["jobs"].values():
        for item in job["steps"]:
            if "uses" not in item:
                continue
            action, revision = item["uses"].split("@")
            assert action in ACTION_SHAS and revision == ACTION_SHAS[action]
            assert re.fullmatch(r"[0-9a-f]{40}", revision)
            observed.add(action)
    assert observed == set(ACTION_SHAS)


def dispatch_guard_result(overrides):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable on this platform; Windows CI executes the actual guard")
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update({
        "INPUT_SOURCE_SHA": "a" * 40, "GITHUB_SHA": "a" * 40,
        "GITHUB_REPOSITORY": "AureliusWu/Agent", "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTIONS": "true",
        "GITHUB_WORKFLOW_REF": "AureliusWu/Agent/.github/workflows/rc-desktop-candidate.yml@refs/heads/main",
        "RUNNER_ENVIRONMENT": "github-hosted",
    })
    environment.update(overrides)
    command = workflow()["jobs"]["build"]["steps"][0]["run"]
    return subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", command],
                          env=environment, capture_output=True, timeout=15, check=False)


def test_actual_powershell_guard_accepts_only_the_expected_dispatch():
    assert dispatch_guard_result({}).returncode == 0


@pytest.mark.parametrize("invalid", [
    {"INPUT_SOURCE_SHA": ""}, {"INPUT_SOURCE_SHA": "main"}, {"INPUT_SOURCE_SHA": "A" * 40},
    {"INPUT_SOURCE_SHA": "b" * 40}, {"GITHUB_SHA": "b" * 40},
    {"GITHUB_REPOSITORY": "fork/Agent"}, {"GITHUB_REF": "refs/heads/development"},
    {"GITHUB_REF": "refs/tags/v16.0.1"}, {"GITHUB_EVENT_NAME": "pull_request"},
    {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_EVENT_NAME": "repository_dispatch"},
    {"GITHUB_WORKFLOW_REF": "AureliusWu/Agent/.github/workflows/release.yml@refs/heads/main"},
    {"GITHUB_WORKFLOW_REF": "AureliusWu/Agent/.github/workflows/rc-desktop-candidate.yml@refs/heads/other"},
    {"GITHUB_ACTIONS": "false"}, {"RUNNER_ENVIRONMENT": "self-hosted"},
])
def test_actual_powershell_guard_refuses_wrong_source_trigger_repository_or_host(invalid):
    assert dispatch_guard_result(invalid).returncode != 0


def test_fixed_build_and_lifecycle_graph_cannot_summarize_a_failed_installer():
    jobs = workflow()["jobs"]
    assert jobs["build"]["name"] == "Build candidate"
    assert jobs["build"]["outputs"] == {"artifact_id": "${{ steps.payload.outputs.artifact-id }}"}
    assert jobs["installed"]["name"] == "Installed lifecycle (${{ matrix.kind }})"
    assert jobs["installed"]["needs"] == "build"
    assert jobs["installed"]["strategy"] == {"fail-fast": False, "matrix": {"kind": ["nsis", "msi"]}}
    assert jobs["summarize"]["needs"] == ["build", "installed"]
    assert jobs["summarize"]["if"] == "${{ success() && needs.build.result == 'success' && needs.installed.result == 'success' }}"
    for name, role in (("build", "build"), ("installed", "${{ matrix.kind }}"), ("summarize", "summarize")):
        helper = next(item for item in jobs[name]["steps"] if "scripts/collect-rc-desktop-candidate.py" in item.get("run", ""))
        assert "-B scripts/collect-rc-desktop-candidate.py " + role in helper["run"]
        expected_environment = {"GH_TOKEN": "${{ github.token }}", "INPUT_SOURCE_SHA": "${{ inputs.source_sha }}"}
        if name != "build":
            expected_environment["CANDIDATE_ARTIFACT_ID"] = "${{ needs.build.outputs.artifact_id }}"
            assert '--artifact-id "$env:CANDIDATE_ARTIFACT_ID"' in helper["run"]
            assert "${{ needs.build.outputs.artifact_id }}" not in helper["run"]
        assert helper["env"] == expected_environment


@pytest.mark.parametrize("job_name", ["installed", "summarize"])
@pytest.mark.parametrize("untrusted_output", ["7; Write-Output UNEXPECTED", "$(Write-Output UNEXPECTED)", "'\"; Write-Output UNEXPECTED; #"])
def test_upstream_artifact_output_is_one_literal_argument_not_powershell_source(job_name, untrusted_output):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell unavailable; Windows CI exercises actual argv expansion")
    item = next(step for step in workflow()["jobs"][job_name]["steps"]
                if "scripts/collect-rc-desktop-candidate.py" in step.get("run", ""))
    script = item["run"].replace("${{ matrix.kind }}", "nsis")
    # Keep the real workflow's quoted output argument; replace only the
    # executable/consumer with a passive argv observer (never install/fetch).
    if script.startswith("python "):
        script = "& $env:VALIDATION_PYTHON" + script[len("python"):]
    else:
        script = script.replace('& "$env:RUNNER_TEMP/desktop-reader-venv/Scripts/python.exe"',
                                "& $env:VALIDATION_PYTHON", 1)
    script = script.replace(" -B scripts/collect-rc-desktop-candidate.py",
                            " -c 'import json,sys;print(json.dumps(sys.argv[1:]))'", 1)
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(VALIDATION_PYTHON=sys.executable, CANDIDATE_ARTIFACT_ID=untrusted_output)
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", script],
                            env=environment, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0
    role = "nsis" if job_name == "installed" else "summarize"
    assert json.loads(result.stdout) == [role, "--artifact-id", untrusted_output]


def test_historical_onefile_reader_is_prepared_from_exact_existing_locks():
    job = workflow()["jobs"]["installed"]
    prepare = step(job, "Prepare the locked historical onefile reader")["run"]
    lock = (ROOT / "siyi/requirements.lock").read_text(encoding="utf-8")
    assert "python -m venv $reader" in prepare and "--no-deps" in prepare
    pins = dict(re.findall(r"\b([a-z][a-z0-9-]*)==([0-9][a-zA-Z0-9.]*)", prepare))
    assert pins == READER_PACKAGES
    for package, version in pins.items():
        assert re.search(r"^" + re.escape(package + "==" + version) + r"(?:\s*;[^\n]*)?$", lock, flags=re.MULTILINE)
    assert "from PyInstaller.archive.readers import CArchiveReader" in prepare
    execute = step(job, "Observe the real owned installed lifecycle")
    assert "desktop-reader-venv/Scripts/python.exe" in execute["run"]
    assert job["steps"].index(step(job, "Prepare the locked historical onefile reader")) < job["steps"].index(execute)


def test_uploads_are_only_validated_public_roots_and_never_a_formal_rc():
    jobs = workflow()["jobs"]
    roots = {"build": "${{ runner.temp }}/candidate-public/",
             "installed": "${{ runner.temp }}/${{ matrix.kind }}-public/",
             "summarize": "${{ runner.temp }}/desktop-summary-public/"}
    for name, job in jobs.items():
        uploads = [item for item in job["steps"] if str(item.get("uses", "")).startswith("actions/upload-artifact@") and "if" not in item]
        assert len(uploads) == 1
        settings = uploads[0]["with"]
        assert settings["path"] == roots[name] and settings["if-no-files-found"] == "error"
        assert settings["retention-days"] == 7
        assert "${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in settings["name"]
        assert "Siyi-RC-Acceptance" not in settings["name"]
        assert "if" not in uploads[0]
    assert next(item for item in jobs["build"]["steps"] if item.get("id") == "payload")["with"]["compression-level"] == 0
    commands = "\n".join(item.get("run", "") for job in jobs.values() for item in job["steps"])
    for forbidden in ("gh release", "git tag", "git push", "--release", "release-gate.ps1", "export-accepted-rc.py", "runtime_file_cli", "model pull", "operator_attested=true"):
        assert forbidden not in commands
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")


def test_failed_jobs_export_only_one_exact_typed_diagnostic_and_remain_failed():
    suffixes = {"build": "build", "installed": "${{ matrix.kind }}", "summarize": "summarize"}
    for name, job in workflow()["jobs"].items():
        failed = [item for item in job["steps"] if item.get("if") == "${{ failure() }}"]
        assert len(failed) == 1
        item = failed[0]
        assert item["uses"] == "actions/upload-artifact@" + ACTION_SHAS["actions/upload-artifact"]
        assert item["with"] == {
            "name": "Siyi-Desktop-Failure-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}-" + suffixes[name],
            "path": "${{ runner.temp }}/desktop-failure-public/failure.json",
            "if-no-files-found": "ignore", "retention-days": 7,
        }
        assert "continue-on-error" not in job
        assert all("continue-on-error" not in step for step in job["steps"])
