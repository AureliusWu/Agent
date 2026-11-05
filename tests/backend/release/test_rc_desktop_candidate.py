"""Candidate transport stays separate from formal acceptance and user data."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import urllib.error
import zipfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


@pytest.fixture
def candidate(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import rc_desktop_candidate
    return rc_desktop_candidate


@pytest.fixture
def expected():
    return {"source_commit": "a" * 40, "run_id": 7, "run_attempt": 2}


@pytest.fixture
def run(candidate, expected):
    return {**expected, "id": 7, "head_sha": expected["source_commit"], "head_branch": "main", "workflow_id": 123,
            "status": "in_progress", "conclusion": None, "event": "workflow_dispatch", "path": candidate.WORKFLOW,
            "run_started_at": "2026-11-05T01:00:00Z", "repository": {"id": 99, "full_name": candidate.REPOSITORY},
            "head_repository": {"id": 99, "full_name": candidate.REPOSITORY}}


@pytest.fixture
def job():
    return {"id": 31, "run_id": 7, "run_attempt": 2, "head_sha": "a" * 40, "name": "Build candidate",
            "status": "completed", "conclusion": "success", "started_at": "2026-11-05T01:01:00Z", "completed_at": "2026-11-05T01:10:00Z"}


@pytest.fixture
def receipt(candidate, expected, run, job):
    identity = candidate.validate_run(run, copy.deepcopy(run), expected)
    artifact = {"id": 83, "name": candidate.artifact_name(expected, "payload"), "expired": False,
        "size_in_bytes": 100, "digest": "sha256:" + "b" * 64, "created_at": "2026-11-05T01:09:00Z",
        "workflow_run": {"id": 7, "repository_id": 99, "head_repository_id": 99, "head_sha": "a" * 40, "head_branch": "main"}}
    return candidate.selection(artifact, identity, job, "payload")


def test_real_active_candidate_run_is_not_a_formal_completed_run(candidate, expected, run):
    identity = candidate.validate_run(run, copy.deepcopy(run), expected)
    assert identity["run_attempt"] == 2
    assert identity["workflow_path"].endswith("rc-desktop-candidate.yml")
    formal = candidate.module("validate-rc-run")
    with pytest.raises(ValueError):
        formal.validate_run(run, repository=candidate.REPOSITORY, commit=expected["source_commit"], run_id=7)


@pytest.fixture
def dispatch_env(candidate, expected):
    return {"INPUT_SOURCE_SHA": expected["source_commit"], "GITHUB_SHA": expected["source_commit"],
        "GITHUB_REPOSITORY": candidate.REPOSITORY, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": candidate.REPOSITORY + "/" + candidate.WORKFLOW + "@refs/heads/main",
        "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "2"}


def test_dispatch_identity_exact_own_main(candidate, expected, dispatch_env):
    assert candidate.environment_identity(dispatch_env) == expected


@pytest.mark.parametrize("field,value", [("INPUT_SOURCE_SHA", "A" * 40), ("GITHUB_SHA", "b" * 40),
    ("GITHUB_REPOSITORY", "someone/Agent"), ("GITHUB_REF", "refs/tags/v16.0.1"), ("GITHUB_EVENT_NAME", "push"),
    ("GITHUB_WORKFLOW_REF", "AureliusWu/Agent/.github/workflows/release.yml@refs/heads/main"),
    ("GITHUB_ACTIONS", "false"), ("RUNNER_ENVIRONMENT", "self-hosted"), ("GITHUB_RUN_ID", "7;echo"), ("GITHUB_RUN_ATTEMPT", "0")])
def test_no_branch_tag_fork_self_hosted_or_wrong_sha_entry(candidate, dispatch_env, field, value):
    with pytest.raises(ValueError):
        candidate.environment_identity({**dispatch_env, field: value})


@pytest.mark.parametrize("field,value", [
    ("run_attempt", 1), ("run_attempt", True), ("id", True), ("head_sha", "b" * 40),
    ("path", ".github/workflows/rc-acceptance.yml"), ("event", "push"), ("head_branch", "other"),
    ("status", "queued"), ("workflow_id", True), ("run_started_at", "2026-11-05"),
])
def test_run_mismatch_hard_fails(candidate, expected, run, field, value):
    changed = {**run, field: value}
    with pytest.raises(ValueError):
        candidate.validate_run(changed, copy.deepcopy(changed), expected)


def test_independent_attempt_metadata_cannot_alias_latest_run(candidate, expected, run):
    old = {**run, "run_started_at": "2026-11-04T01:00:00Z"}
    with pytest.raises(ValueError, match="independent attempt"):
        candidate.validate_run(run, old, expected)


def test_fork_and_terminal_failure_rejected(candidate, expected, run):
    for changed in ({**run, "head_repository": {"id": 100, "full_name": candidate.REPOSITORY}},
                    {**run, "status": "completed", "conclusion": "failure"}):
        with pytest.raises(ValueError):
            candidate.validate_run(changed, changed, expected)


@pytest.mark.parametrize("field,value", [("run_attempt", 1), ("head_sha", "b" * 40), ("conclusion", "failure"), ("status", "in_progress"), ("name", "collect"), ("id", True)])
def test_exact_producer_job_required(candidate, expected, job, field, value):
    with pytest.raises(ValueError):
        candidate.select_job([{**job, field: value}], expected, "payload", completed=True)


def test_ambiguous_producer_rejected(candidate, expected, job):
    with pytest.raises(ValueError):
        candidate.select_job([job, {**job, "id": 32}], expected, "payload", completed=True)


def test_artifact_selection_exact_job_attempt_time(candidate, receipt):
    assert receipt["producer_job_id"] == 31
    assert receipt["report_type"].endswith("not_acceptance")
    assert receipt["artifact"]["id"] == 83


@pytest.mark.parametrize("field,value", [("expired", True), ("digest", None), ("size_in_bytes", True),
    ("created_at", "2026-11-05T01:00:59Z"), ("created_at", "2026-11-05T01:10:01Z"), ("name", "Siyi-RC-Acceptance-v16.0.1")])
def test_artifact_metadata_rejected(candidate, expected, run, job, field, value):
    identity = candidate.validate_run(run, run, expected)
    artifact = {"id": 83, "name": candidate.artifact_name(expected, "payload"), "expired": False,
        "size_in_bytes": 100, "digest": "sha256:" + "b" * 64, "created_at": "2026-11-05T01:09:00Z",
        "workflow_run": {"id": 7, "repository_id": 99, "head_repository_id": 99, "head_sha": "a" * 40, "head_branch": "main"}}
    artifact[field] = value
    with pytest.raises(ValueError):
        candidate.selection(artifact, identity, job, "payload")


@pytest.mark.parametrize("field,value", [("id", 8), ("repository_id", 100), ("head_repository_id", 100), ("head_sha", "c" * 40), ("head_branch", "other")])
def test_foreign_artifact_origin_rejected(candidate, expected, run, job, field, value):
    identity = candidate.validate_run(run, run, expected)
    artifact = {"id": 83, "name": candidate.artifact_name(expected, "payload"), "expired": False,
        "size_in_bytes": 100, "digest": "sha256:" + "b" * 64, "created_at": "2026-11-05T01:09:00Z",
        "workflow_run": {"id": 7, "repository_id": 99, "head_repository_id": 99, "head_sha": "a" * 40, "head_branch": "main"}}
    artifact["workflow_run"][field] = value
    with pytest.raises(ValueError):
        candidate.selection(artifact, identity, job, "payload")


def archive(candidate, path, receipt, *, files=None, change=None, extra=None):
    files = files or {candidate.BUILD: b'{}'}
    entries = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()]
    index = {"schema_version": 1, "report_type": "desktop_candidate_transport_not_acceptance", "target_version": "16.0.1",
             "accepted_candidate": False, "role": receipt["role"], "run": receipt["run"],
             "producer_job_id": receipt["producer_job_id"], "files": entries}
    if change:
        change(index)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        stream.writestr(candidate.INDEX, json.dumps(index))
        for name, data in files.items():
            stream.writestr(name, data)
        if extra:
            stream.writestr(*extra)
    return path


def test_nonformal_archive_roundtrip_never_accepted(candidate, receipt, tmp_path, monkeypatch):
    output = archive(candidate, tmp_path / "input.zip", receipt)
    monkeypatch.setattr(candidate, "ROOT", tmp_path / "checkout")
    index = candidate.receive(output, receipt)
    assert index["accepted_candidate"] is False
    assert (candidate.ROOT / candidate.BUILD).read_bytes() == b'{}'
    with pytest.raises(ValueError):
        candidate.module("export-accepted-rc").validate_archive(output, "a" * 40)
    with pytest.raises(ValueError, match="overwrite"):
        candidate.receive(output, receipt)


@pytest.mark.parametrize("mutation", [
    lambda index: index.update(accepted_candidate=True),
    lambda index: index.update(producer_job_id=32),
    lambda index: index["run"].update(run_attempt=1),
    lambda index: index["files"][0].update(sha256="c" * 64),
    lambda index: index["files"][0].update(bytes=True),
    lambda index: index["files"].append(index["files"][0]),
])
def test_index_failures_before_extraction(candidate, receipt, tmp_path, mutation):
    original = copy.deepcopy(receipt)
    output = archive(candidate, tmp_path / "input.zip", receipt, change=mutation)
    with pytest.raises(ValueError):
        candidate.validate_zip(output, original)


@pytest.mark.parametrize("name", ["../escape", "C" + ":/escape", "desktop/src-tauri/target/release/private/file.json", "siyi/app/main.py", "build/v1601-evidence/accepted/rc-bundle.json", "desktop/src-tauri/target/release/ci-candidate/portable/_internal/.env", "desktop/src-tauri/target/release/ci-candidate/portable/_internal/data.db"])
def test_private_source_and_traversal_paths_rejected(candidate, receipt, tmp_path, name):
    output = archive(candidate, tmp_path / "input.zip", receipt, files={name: b'{}'})
    with pytest.raises(ValueError):
        candidate.validate_zip(output, receipt)


def test_duplicate_case_link_directory_and_unindexed_members(candidate, receipt, tmp_path):
    for label, extra in [("case", (candidate.BUILD.upper(), b'{}')), ("extra", (candidate.INVENTORY, b'{}')),
                         ("directory", (candidate.PREFIX + "/", b''))]:
        output = archive(candidate, tmp_path / (label + ".zip"), receipt, extra=extra)
        with pytest.raises(ValueError):
            candidate.validate_zip(output, receipt)
    link = zipfile.ZipInfo(candidate.INVENTORY)
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    output = archive(candidate, tmp_path / "link.zip", receipt, extra=(link, b"elsewhere"))
    with pytest.raises(ValueError):
        candidate.validate_zip(output, receipt)


def test_private_database_and_credential_content_rejected(candidate, receipt, tmp_path):
    for number, content in enumerate((b"SQLite format 3\x00", json.dumps(dict(password="synthetic")).encode())):
        output = archive(candidate, tmp_path / f"private-{number}.zip", receipt, files={candidate.BUILD: content})
        with pytest.raises(ValueError):
            candidate.validate_zip(output, receipt)


def test_zip_byte_and_count_bounds(candidate, receipt, tmp_path, monkeypatch):
    transport = candidate.module("export-accepted-rc")
    output = archive(candidate, tmp_path / "input.zip", receipt)
    monkeypatch.setattr(transport, "MAX_FILES", 0)
    with pytest.raises(ValueError, match="count"):
        candidate.validate_zip(output, receipt)
    monkeypatch.setattr(transport, "MAX_FILES", 20000)
    monkeypatch.setattr(transport, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="bytes"):
        candidate.validate_zip(output, receipt)


def test_download_drift_prevents_any_extraction(candidate, expected, run, job, receipt, tmp_path, monkeypatch):
    identity = candidate.validate_run(run, run, expected)
    artifact = {**receipt["artifact"], "expired": False,
                "workflow_run": {"id": 7, "repository_id": 99, "head_repository_id": 99, "head_sha": "a" * 40, "head_branch": "main"}}
    responses = iter((artifact, {**artifact, "digest": "sha256:" + "c" * 64}))
    monkeypatch.setattr(candidate, "current", lambda expected: identity)
    monkeypatch.setattr(candidate, "jobs", lambda expected: [job])
    monkeypatch.setattr(candidate, "api", lambda endpoint: next(responses))
    downloaded = []
    monkeypatch.setattr(candidate.module("export-accepted-rc"), "download_action_artifact", lambda *args: downloaded.append(args))
    monkeypatch.setattr(candidate, "receive", lambda *args: pytest.fail("drift must not reach extraction"))
    with pytest.raises(ValueError, match="drift"):
        candidate.fetch(expected, "payload", tmp_path, artifact_id=83)
    assert len(downloaded) == 1
    assert downloaded[0][1:4] == (83, "b" * 64, 100)


def test_main_move_blocks_before_artifact_fetch(candidate, expected, monkeypatch):
    monkeypatch.setattr(candidate, "api", lambda endpoint: {"ref": "refs/heads/main", "object": {"sha": "b" * 40}})
    with pytest.raises(ValueError, match="main moved"):
        candidate.current(expected)


@pytest.mark.parametrize("pages", [
    [{"total_count": 2, "jobs": [{"id": 1}]}, {"total_count": 3, "jobs": [{"id": 2}]}],
    [{"total_count": 2, "jobs": [{"id": 1}]}, {"total_count": 2, "jobs": []}],
    [{"total_count": 2, "jobs": [{"id": 1}, {"id": 1}]}],
    [{"total_count": True, "jobs": []}],
])
def test_pagination_changed_truncated_duplicate_or_untyped_fails(candidate, monkeypatch, pages):
    rows = iter(pages)
    monkeypatch.setattr(candidate, "api", lambda endpoint: next(rows))
    with pytest.raises(ValueError):
        candidate.pages("/unit", "jobs")


def test_failed_byte_hash_never_reaches_extraction(candidate, expected, run, job, receipt, tmp_path, monkeypatch):
    identity = candidate.validate_run(run, run, expected)
    artifact = {**receipt["artifact"], "expired": False,
                "workflow_run": {"id": 7, "repository_id": 99, "head_repository_id": 99, "head_sha": "a" * 40, "head_branch": "main"}}
    monkeypatch.setattr(candidate, "current", lambda expected: identity)
    monkeypatch.setattr(candidate, "jobs", lambda expected: [job])
    monkeypatch.setattr(candidate, "api", lambda endpoint: artifact)
    def fail(*args):
        raise ValueError("actual ZIP digest mismatch")
    monkeypatch.setattr(candidate.module("export-accepted-rc"), "download_action_artifact", fail)
    monkeypatch.setattr(candidate, "receive", lambda *args: pytest.fail("wrong bytes must not reach extraction"))
    with pytest.raises(ValueError, match="digest"):
        candidate.fetch(expected, "payload", tmp_path, artifact_id=83)


def test_fixed_prior_download_uses_original_transport_and_exact_bytes(candidate, tmp_path, monkeypatch):
    spec = candidate.PRIOR["nsis"]
    meta = {"id": spec["id"], "name": spec["name"], "size": spec["size"], "digest": "sha256:" + spec["sha256"],
            "state": "uploaded", "url": f"https://api.github.com/repos/{candidate.REPOSITORY}/releases/assets/{spec['id']}"}
    release = {"tag_name": "v8.0.1", "draft": False, "prerelease": False, "assets": [meta]}
    monkeypatch.setattr(candidate, "ROOT", tmp_path)
    monkeypatch.setenv("GH_TOKEN", "unit-fixture")
    monkeypatch.setattr(candidate, "api", lambda endpoint: release if "/tags/" in endpoint else meta)
    captured = []
    def download(opener, request, size, sha, output):
        captured.append((opener, request, size, sha, output))
        output.parent.mkdir(parents=True)
        output.write_bytes(b"unit-only")
    monkeypatch.setattr(candidate.module("export-accepted-rc"), "download_exact", download)
    previous, path = candidate.download_prior("nsis")
    assert captured[0][2:4] == (spec["size"], spec["sha256"])
    assert any(isinstance(handler, candidate.module("export-accepted-rc").AssetRedirects) for handler in captured[0][0].handlers)
    assert candidate.read(path)["assets"][0]["github_asset_digest"] == meta["digest"]
    assert previous.name == spec["name"]


def test_no_workflow_credentials_in_build_children(candidate, monkeypatch):
    spec = importlib.util.spec_from_file_location("candidate_collector_test", SCRIPTS / "collect-rc-desktop-candidate.py")
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "DEEPSEEK_API_KEY", "AGENT_PROVIDER_CONFIG_PATH"):
        monkeypatch.setenv(key, "unit-fixture")
    environment = collector.process_environment()
    assert not any(key in environment for key in ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "DEEPSEEK_API_KEY", "AGENT_PROVIDER_CONFIG_PATH"))
    assert environment["SIYI_ALLOW_PAID_API"] == "false"
    assert environment["HF_HUB_OFFLINE"] == "1"


@pytest.mark.parametrize("code,success", [(0, True), (7, False)])
def test_raw_execution_and_output_are_retained_privately_without_rewritten_argv(candidate, tmp_path, capsys, code, success):
    collector = collector_module()
    command = [sys.executable, "-c", f"print('synthetic private output'); raise SystemExit({code})"]
    if success:
        references = collector.execute(command, tmp_path, tmp_path, "probe", timeout=10)
        assert references["raw_execution_sha256"] == candidate.digest(tmp_path / "probe-execution.json")
    else:
        with pytest.raises(ValueError):
            collector.execute(command, tmp_path, tmp_path, "probe", timeout=10)
    execution = candidate.read(tmp_path / "probe-execution.json")
    assert execution["command"] == command and execution["cwd"] == str(tmp_path)
    assert execution["exit_code"] == code and execution["actual_run"] is True
    assert execution["timed_out"] is False
    assert "synthetic private output" in (tmp_path / "probe.log").read_text(encoding="utf-8")
    assert "synthetic private output" not in capsys.readouterr().out


def test_execution_timeout_and_start_failure_never_claim_a_successful_run(candidate, tmp_path):
    collector = collector_module()
    with pytest.raises(subprocess.TimeoutExpired):
        collector.execute([sys.executable, "-c", "import time; time.sleep(2)"], tmp_path, tmp_path, "timeout", timeout=.05)
    timed_out = candidate.read(tmp_path / "timeout-execution.json")
    assert timed_out["timed_out"] is True and timed_out["exit_code"] is None
    with pytest.raises(OSError):
        collector.execute([str(tmp_path / "nonexistent-program.exe")], tmp_path, tmp_path, "missing", timeout=1)
    missing = candidate.read(tmp_path / "missing-execution.json")
    assert missing["actual_run"] is False and missing["exit_code"] is None


def collector_module():
    spec = importlib.util.spec_from_file_location("candidate_collector_matrix_test", SCRIPTS / "collect-rc-desktop-candidate.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def pe_file(path, marker=b""):
    """Only protocol-test PE-shaped bytes; no application is executed."""
    content = bytearray(1024**2)
    content[:2] = b"MZ"
    content[60:64] = (64).to_bytes(4, "little")
    content[64:68] = b"PE\0\0"
    content[128:128 + len(marker)] = marker
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def synthetic_desktop_matrix(candidate, expected, tmp_path, monkeypatch):
    """Test the matrix codec separately from already independently tested live receipts."""
    collector = collector_module()
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    monkeypatch.setattr(candidate, "ROOT", tmp_path)
    paths = {"desktop": tmp_path / candidate.PORTABLE / "司忆.exe", "sidecar": tmp_path / candidate.PORTABLE / "agent-backend.exe"}
    pe_file(paths["desktop"], b"__TAURI_BUNDLE_TYPE_VAR_UNK")
    pe_file(paths["sidecar"])
    binaries = {key: candidate.reference(path) for key, path in paths.items()}
    source = {"source_version": "16.0.1", "source_commit": expected["source_commit"], "workspace_clean": True, "source_tree_fingerprint": "c" * 64}
    manifest = {"build_id": "d" * 24}
    payload = {"payload_content_sha256": "e" * 64}
    monkeypatch.setattr(collector, "validate_payload", lambda expected: (source, manifest, binaries, payload, {}))
    monkeypatch.setattr(candidate.module("rc_installed_public"), "validate_public_report", lambda *a, **k: None)
    observed_formats = []
    monkeypatch.setattr(candidate.module("check-release-evidence"), "_validate_one", lambda *a, **k: observed_formats.append(k["kind"]))
    original_read = collector.read
    monkeypatch.setattr(collector, "read", lambda path: {} if path.name.endswith("installer-smoke.json") else original_read(path))
    gate = candidate.module("rc_gate")
    for kind in ("nsis", "msi"):
        checks = {gate.requirement_id("desktop", name): True for name in ("tauri_build", kind, "install", "launch", "sidecar_health", "upgrade", "uninstall")}
        candidate.write(tmp_path / candidate.PREFIX / (kind + "-checks.json"), {
            "report_type": "rc_check_evidence", "target_version": "16.0.1", "kind": "desktop", "status": "PASS", "actual_run": True,
            "source": source, "source_after": source, "build_id": manifest["build_id"],
            "binary_sha256": gate.installer_binary_hashes(tmp_path, binaries, kind.upper()),
            "artifact_variant": kind, "sidecar_payload_sha256": payload["payload_content_sha256"], "checks": checks})
    return collector, tmp_path / candidate.PREFIX / "nsis-checks.json", observed_formats


def test_original_matrix_contract_validates_all_8_and_keeps_other_gates_blocked(candidate, expected, synthetic_desktop_matrix):
    collector, _, formats = synthetic_desktop_matrix
    matrix, pending, _ = collector.desktop_matrix(expected)
    assert len(matrix["release_gates"]["desktop"]) == 8
    assert set(formats) == {"NSIS", "MSI"}
    assert pending and all("AUTOMATED" in error or "MANUAL" in error or "automated" in error or "manual" in error for error in pending)
    assert not any("operator_attested" in json.dumps(record) for record in matrix["release_gates"]["desktop"])


@pytest.mark.parametrize("field,value", [("build_id", "f" * 24), ("artifact_variant", "portable"), ("sidecar_payload_sha256", "f" * 64),
    ("kind", "manual"), ("actual_run", False), ("checks", {})])
def test_real_matrix_validator_rejects_wrong_desktop_evidence_binding(candidate, expected, synthetic_desktop_matrix, field, value):
    collector, path, _ = synthetic_desktop_matrix
    raw = candidate.read(path)
    raw[field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="original specialized contract"):
        collector.desktop_matrix(expected)


def test_unknown_original_validator_error_is_never_filtered_as_pending(candidate, expected, synthetic_desktop_matrix, monkeypatch):
    collector, _, _ = synthetic_desktop_matrix
    monkeypatch.setattr(candidate.module("rc_gate"), "validate_matrix", lambda *a, **k: ["future unknown contract error"])
    with pytest.raises(ValueError, match="original specialized contract"):
        collector.desktop_matrix(expected)


@pytest.mark.parametrize("mutation", [None, "bundle_payload_drift", "candidate_hook"])
def test_candidate_build_orchestration_pins_full_payload_and_excludes_restored_placeholder(candidate, expected, run, job, tmp_path, monkeypatch, mutation):
    """Mock compiler boundaries; exercise actual staging/privacy/byte validators."""
    collector = collector_module()
    root, private, runner_temp = tmp_path / "checkout", tmp_path / "private", tmp_path / "runner-temp"
    root.mkdir()
    private.mkdir()
    runner_temp.mkdir()
    monkeypatch.setattr(collector, "ROOT", root)
    monkeypatch.setattr(candidate, "ROOT", root)
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "native-system"))
    identity = candidate.validate_run(run, run, expected)
    before = {"source_version": "16.0.1", "source_commit": expected["source_commit"], "workspace_clean": True, "source_tree_fingerprint": "c" * 64}
    monkeypatch.setattr(collector, "current", lambda expected: identity)
    monkeypatch.setattr(collector, "jobs", lambda expected: [{**job, "status": "in_progress", "conclusion": None}])
    monkeypatch.setattr(collector, "source", lambda expected: before)
    monkeypatch.setattr(collector, "disk_preflight", lambda *args: None)
    monkeypatch.setattr(collector, "build_shell", lambda private: "synthetic-pwsh.exe")
    candidate.write(root / "desktop/src-tauri/tauri.conf.json", {"build": {}, "bundle": {"resources": {"binaries/_internal/": "_internal/"}}})
    manifest = {"manifest_version": 1, "product_version": "16.0.1", "workspace_state": "CLEAN", "build_type": "Release",
        "git_commit": expected["source_commit"], "source_fingerprint": before["source_tree_fingerprint"], "build_id": "d" * 24,
        "database_schema_version": 46, "component_build_ids": {name: name + "-" + "d" * 24 for name in ("tauri", "react", "sidecar")}}
    original = root / "build/candidates/ci-desktop-7-2"
    calls = []
    def fake_execute(argv, cwd, private, label, **kwargs):
        calls.append(label)
        if label == "candidate-build":
            portable = original / "portable"
            pe_file(portable / "司忆.exe", b"__TAURI_BUNDLE_TYPE_VAR_UNK")
            pe_file(portable / "agent-backend.exe")
            candidate.write(portable / "_internal/build-info.json", manifest)
            (portable / "_internal/code.bin").write_bytes(b"synthetic frozen bytes")
            candidate.write(original / "build-info.json", manifest)
            overlay = {"build": {"frontendDist": "../../build/candidates/ci-desktop-7-2/frontend-dist", "beforeBuildCommand": "npm --prefix ../frontend run build:desktop"}}
            if mutation == "candidate_hook":
                overlay["build"]["beforeBundleCommand"] = "unreviewed"
            candidate.write(original / "tauri-candidate.json", overlay)
            artifacts = [{"path": name, "sha256": candidate.digest(original / name)} for name in
                         ("portable/司忆.exe", "portable/agent-backend.exe", "portable/_internal/build-info.json")]
            candidate.write(original / "candidate-build.json", {"actual_run": True, "status": "BUILT", "workspace_state": "CLEAN",
                "source_commit": expected["source_commit"], "build_id": manifest["build_id"], "artifacts": artifacts})
            destination = root / "desktop/src-tauri/target/release/司忆.exe"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((portable / "司忆.exe").read_bytes())
            external = root / "desktop/src-tauri/binaries/agent-backend-x86_64-pc-windows-msvc.exe"
            external.parent.mkdir(parents=True, exist_ok=True)
            external.write_bytes((portable / "agent-backend.exe").read_bytes())
            (external.parent / "_internal").mkdir()
            (external.parent / "_internal/.gitkeep").write_bytes(b"\n")
        else:
            overlay = candidate.read(private / "bundle-inputs.json")
            assert overlay["bundle"]["resources"] == {"binaries/_internal/": None, (original / "portable/_internal").as_posix() + "/": "_internal/"}
            assert "--no-bundle" not in argv and "--ci" in argv
            if mutation == "bundle_payload_drift":
                (original / "portable/_internal/code.bin").write_bytes(b"changed")
            for kind, name in (("nsis", "Synthetic_16.0.1_x64-setup.exe"), ("msi", "Synthetic_16.0.1_x64.msi")):
                pe_file(root / "desktop/src-tauri/target/release/bundle" / kind / name)
        return {"raw_execution_sha256": "1" * 64, "raw_log_sha256": "2" * 64}
    monkeypatch.setattr(collector, "execute", fake_execute)
    if mutation:
        with pytest.raises(ValueError):
            collector.build(expected, private)
        assert not (runner_temp / "candidate-public").exists()
    else:
        collector.build(expected, private)
        index = candidate.read(runner_temp / "candidate-public" / candidate.INDEX)
        assert index["accepted_candidate"] is False
        assert len([row for row in index["files"] if "/bundle/" in row["path"]]) == 2
        assert not any(row["path"].endswith(".gitkeep") for row in index["files"])
        assert candidate.read(root / candidate.INVENTORY)["file_count"] == 2
        assert calls == ["candidate-build", "candidate-bundle"]


@pytest.mark.parametrize("major,allowed", [(b"7\r\n", True), (b"8\n", True), (b"5\r\n", False), (b"6\n", False), (b"", False), (b"7;exit 0", False), (b"7" * 33, False)])
def test_candidate_build_requires_fixed_trusted_pwsh7_and_real_version_probe(candidate, tmp_path, monkeypatch, major, allowed):
    collector = collector_module()
    program_files = tmp_path / "program-files"
    shell = program_files / "PowerShell/7/pwsh.exe"
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b"synthetic never executed")
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    def fake_execute(argv, cwd, private, label, **kwargs):
        assert argv[0] == str(shell) and argv[-1] == "$PSVersionTable.PSVersion.Major"
        assert "powershell.exe" not in argv[0].lower()
        assert label == "candidate-shell-probe"
        (private / "candidate-shell-probe.log").write_bytes(major)
    monkeypatch.setattr(collector, "execute", fake_execute)
    if allowed:
        assert collector.build_shell(tmp_path) == str(shell)
    else:
        with pytest.raises(ValueError, match="PowerShell 7"):
            collector.build_shell(tmp_path)


def test_candidate_build_never_falls_back_to_system_powershell5(candidate, tmp_path, monkeypatch):
    collector = collector_module()
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setattr(collector, "execute", lambda *a, **k: pytest.fail("missing fixed shell must not execute anything"))
    with pytest.raises(OSError):
        collector.build_shell(tmp_path)


def test_failure_projection_keeps_reviewed_guard_codes_not_raw_details(candidate):
    value = candidate.failure_projection("nsis", "native_preflight", candidate.CandidateError("visible native window station unavailable"))
    assert value["guard_code"] == "NATIVE_WINDOW_STATION_UNAVAILABLE"
    assert value["status"] == "BLOCKED" and value["formal_rc_accepted"] is False
    secret = "unit-" + "private-value"
    changed = candidate.failure_projection("nsis", "native_preflight", candidate.CandidateError("visible native window station unavailable " + secret))
    assert changed["guard_code"] == "GUARD_REJECTED"
    assert secret not in json.dumps(changed)


@pytest.mark.parametrize("kind", ["value", "http", "unknown", "no_stringify"])
def test_error_token_private_path_url_and_untrusted_class_never_leak(candidate, tmp_path, kind):
    secret = "gh" + "p_" + "x" * 36
    private = str(tmp_path / "private-profile/data.db")
    url = "https://example.invalid/download?token=" + secret
    message = private + " " + url + " " + secret
    if kind == "value":
        error = ValueError(message)
    elif kind == "http":
        error = urllib.error.HTTPError(url, 403, message, {"Authorization": secret}, None)
    elif kind == "unknown":
        error = type("PrivateClass" + secret, (Exception,), {})(message)
    else:
        class NoStringify(Exception):
            def __str__(self):
                raise AssertionError("public projection must never stringify")
        error = NoStringify(message)
    projected = candidate.failure_projection("build", "candidate_transport", error)
    encoded = json.dumps(projected)
    assert secret not in encoded and private not in encoded and url not in encoded
    assert "Authorization" not in encoded and "message" not in projected and "detail" not in projected
    if kind == "http":
        assert projected["http_status"] == 403 and projected["guard_code"] == "HTTP_REQUEST_FAILED"


def test_injected_diagnostic_metadata_is_strictly_typed_and_bounded(candidate, tmp_path):
    secret = str(tmp_path / "private") + " https://example.invalid/" + "unit-secret"
    values = {"subprocess": {"label": secret, "actual_run": True, "exit_code": "1603 " + secret,
                            "timed_out": False, "log_sha256": "a" * 64, "argv": [secret], "raw_log": secret},
        "native": {"visible_window_station": False, "input_desktop_opened": secret, "full_path": secret},
        "host": {"is_admin": True, "installer_busy": False, "webviews": [secret], "running": [secret, secret],
                 "shortcuts": secret, "installations": [secret], "private": secret},
        "disk": {"free_bytes": -1, "minimum_bytes": 8 * 1024**3, "path": secret},
        "lifecycle": {"last_completed_stage": "launch_previous", "state": "UNCERTAIN_OPERATOR_RECOVERY",
                      "actual_run": True, "fixture_retained": True, "failure_type": "SafetyError", "logs": secret},
        "owned_dispatch": {"exit_code": 1603, "actual_run": True, "installer_or_application_effect": True,
            "command": [secret], "process_cleanup": {"forced_termination": False, "active_after_cleanup": 0, "private": secret}}}
    result = candidate.failure_projection("msi", "installed_lifecycle", ValueError(secret), values)
    assert secret not in json.dumps(result)
    assert result["subprocess"]["exit_code"] is None and result["subprocess"]["label"] == "unknown"
    assert result["subprocess"]["log_sha256"] == "a" * 64
    assert result["host"]["webviews_count"] == 1 and result["host"]["running_count"] == 2
    assert result["host"]["shortcuts_count"] is None
    assert result["native"]["input_desktop_opened"] is None and result["disk"]["free_bytes"] is None
    assert result["lifecycle"]["last_completed_stage"] == "launch_previous"
    assert result["lifecycle"]["uncertain_operator_recovery"] is True
    assert result["owned_dispatch"]["exit_code"] == 1603


@pytest.mark.parametrize("key,value", [("exit_code", True), ("exit_code", 2**40), ("timed_out", "false"), ("actual_run", 1), ("log_sha256", "a" * 63 + "/")])
def test_public_subprocess_diagnostics_reject_untyped_or_excessive_fields(candidate, key, value):
    process = {"label": "candidate-build", "exit_code": 1, "timed_out": False, "actual_run": True, "log_sha256": "a" * 64}
    process[key] = value
    result = candidate.failure_projection("build", "candidate_build", ValueError(), {"subprocess": process})
    assert result["subprocess"][key] is None


@pytest.mark.parametrize("code", [1603, 3221225477, -1073741819])
def test_real_windows_exit_codes_are_retained_without_converting_them_to_success(candidate, code):
    result = candidate.failure_projection("build", "candidate_build", ValueError(), {"subprocess": {"exit_code": code}})
    assert result["subprocess"]["exit_code"] == code and result["status"] == "BLOCKED"


def test_failure_writer_uses_only_fixed_public_json_and_main_exit_remains_one(candidate, expected, tmp_path, monkeypatch, capsys):
    collector = collector_module()
    private = tmp_path / "owned"
    private.mkdir()
    secret = "unit-token-" + "private-path-and-url"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(collector, "environment_identity", lambda env: expected)
    monkeypatch.setattr(collector, "private_directory", lambda role: private)
    def fail(expected):
        raise ValueError(secret)
    monkeypatch.setattr(collector, "source", fail)
    assert collector.main(["build"]) == 1
    assert secret in (private / "failure.json").read_text(encoding="utf-8")
    public = tmp_path / "desktop-failure-public/failure.json"
    assert public.is_file() and secret not in public.read_text(encoding="utf-8")
    assert secret not in capsys.readouterr().out
    assert candidate.read(public)["stage"] == "source"
    assert [path.name for path in public.parent.iterdir()] == ["failure.json"]


def test_malformed_artifact_argument_never_echoes_token_url_or_private_path(candidate, tmp_path, monkeypatch, capsys):
    collector = collector_module()
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    secret = "https://example.invalid/" + str(tmp_path / "private") + "?token=unit-private"
    assert collector.main(["nsis", "--artifact-id", secret]) == 1
    output = capsys.readouterr()
    assert secret not in output.out and secret not in output.err
    result = candidate.read(tmp_path / "desktop-failure-public/failure.json")
    assert result["guard_code"] == "CLI_INPUT_REJECTED" and result["status"] == "BLOCKED"


def test_failure_projection_lifecycle_enum_tracks_original_protocol(candidate):
    assert candidate.LIFECYCLE_STAGES == candidate.module("rc_installed_lifecycle").STAGES


def test_safety_codes_match_only_reviewed_require_literals_and_exact_error_type(candidate):
    lifecycle = candidate.module("rc_installed_lifecycle")
    message = "installed namespace differs from exact self-owned stage"
    assert candidate.safety_code(lifecycle.SafetyError(message), lifecycle.SafetyError) == "SAFETY_" + hashlib.sha256(message.encode()).hexdigest()
    assert candidate.safety_code(ValueError(message), lifecycle.SafetyError) == "UNKNOWN"
    assert candidate.safety_code(lifecycle.SafetyError(message + " private-extra"), lifecycle.SafetyError) == "UNKNOWN"
    # This direct raise is intentionally not a reviewed require(..., literal).
    assert candidate.safety_code(lifecycle.SafetyError("actual installed desktop readiness timed out"), lifecycle.SafetyError) == "UNKNOWN"


def test_observed_transition_preserves_original_result_and_exception(candidate, tmp_path):
    collector = collector_module()
    original = candidate.module("rc_installed_lifecycle")
    result = object()
    error = original.SafetyError("exact product registration and install path required")
    class Base:
        def __init__(self): self.plan = SimpleNamespace(output=tmp_path / "lifecycle.json")
        def transition(self, stage):
            if stage == "install_previous": raise error
            return result
    Observed, _ = collector.observing_types(SimpleNamespace(Lifecycle=Base, WindowsAdapter=object, SafetyError=original.SafetyError))
    observed = Observed()
    assert observed.transition("launch_previous") is result
    with pytest.raises(original.SafetyError) as captured:
        observed.transition("install_previous")
    assert captured.value is error
    assert collector.DIAGNOSTICS["lifecycle_guard"] == {"failed_stage": "install_previous", "guard_code": candidate.safety_code(error, original.SafetyError)}
    assert candidate.read(tmp_path / "private-transition-failure.json")["message"] == error.args[0]


def test_real_lifecycle_uncertain_retention_and_retry_guard_are_unchanged(candidate, tmp_path):
    collector = collector_module()
    spec = importlib.util.spec_from_file_location("observer_original_synthetic_fixture", Path(__file__).with_name("test_rc_installed_lifecycle_v160.py"))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    plan = fixture.plan.__wrapped__(tmp_path)
    class WrongPublisher(fixture.SyntheticAdapter):
        def observe(self, owned):
            value = super().observe(owned)
            if value["installations"]:
                value["installations"][0]["publisher"] = "synthetic foreign publisher"
            return value
    Observed, _ = collector.observing_types(fixture.lifecycle)
    adapter = WrongPublisher(plan.source)
    runner = Observed(plan, adapter)
    result = runner.run()
    assert result["status"] == "BLOCKED" and result["state"] == "UNCERTAIN_OPERATOR_RECOVERY"
    assert result["fixture_retained"] is True and result["automatic_registry_or_fixture_cleanup"] is False
    assert runner.fixture.root.exists() and runner.fixture.install.exists() and runner.uncertain is True
    assert collector.DIAGNOSTICS["lifecycle_guard"]["failed_stage"] == "install_previous"
    assert collector.DIAGNOSTICS["lifecycle_guard"]["guard_code"] != "UNKNOWN"
    count = len(adapter.invocations)
    with pytest.raises(fixture.lifecycle.SafetyError):
        runner.transition("install_previous")
    assert len(adapter.invocations) == count


@pytest.mark.parametrize("failure", [None, "timeout", "safety"])
def test_observed_mutation_preserves_super_result_raise_and_is_not_overwritten_by_probe(candidate, tmp_path, failure):
    collector = collector_module()
    lifecycle = candidate.module("rc_installed_lifecycle")
    error = TimeoutError("synthetic private timeout") if failure == "timeout" else lifecycle.SafetyError("owned dispatcher failed, rebooted, timed out or required forced cleanup")
    result, environment = object(), {"private": "synthetic credential"}
    class Base:
        def __init__(self): self.probes, self.count, self.calls = tmp_path, 0, []
        def owned_command(self, executable, arguments, received, *, timeout=120, mutation=False):
            self.calls.append((executable, arguments, received, timeout, mutation))
            self.count += 1
            candidate.write(self.probes / f"owned-command-{self.count}.json", {"actual_run": True,
                "command": ["https://example.invalid/?token=synthetic-private"], "installer_or_application_effect": mutation,
                "failure_type": "TimeoutError" if failure == "timeout" and mutation else None,
                "exit_code": None if failure == "timeout" and mutation else 1603 if mutation else 0,
                "process_cleanup": {"forced_termination": failure == "timeout" and mutation, "active_after_cleanup": 0,
                    "job_handle_closed": True, "unassigned_cleanup_complete": True,
                    "owned_process_handles_remaining": 0, "owned_thread_handles_remaining": 0}})
            if failure is not None and mutation: raise error
            return result
    _, Observed = collector.observing_types(SimpleNamespace(Lifecycle=object, WindowsAdapter=Base, SafetyError=lifecycle.SafetyError))
    observed = Observed()
    if failure is None:
        assert observed.owned_command("unchanged-program", ["unchanged"], environment, timeout=17, mutation=True) is result
    else:
        with pytest.raises(type(error)) as captured:
            observed.owned_command("unchanged-program", ["unchanged"], environment, timeout=17, mutation=True)
        assert captured.value is error
    raw_mutation = copy.deepcopy(collector.DIAGNOSTICS["last_mutation"])
    assert observed.calls[0][2] is environment and observed.calls[0][3:] == (17, True)
    assert observed.owned_command("native-probe", [], environment, mutation=False) is result
    assert collector.DIAGNOSTICS["last_mutation"] == raw_mutation
    public = candidate.failure_projection("msi", "installed_lifecycle", ValueError(), collector.DIAGNOSTICS)
    assert public["last_mutation"]["exit_code"] == (None if failure == "timeout" else 1603)
    assert public["last_mutation"]["timed_out"] is (failure == "timeout")
    assert "synthetic-private" not in json.dumps(public) and "command" not in public["last_mutation"]


def test_unknown_transition_message_and_forged_code_cannot_leak_or_become_reviewed(candidate, tmp_path):
    collector = collector_module()
    lifecycle = candidate.module("rc_installed_lifecycle")
    secret = "https://example.invalid/?token=" + str(tmp_path / "private-profile")
    error = lifecycle.SafetyError(secret)
    class Base:
        def __init__(self): self.plan = SimpleNamespace(output=tmp_path / "lifecycle.json")
        def transition(self, stage): raise error
    Observed, _ = collector.observing_types(SimpleNamespace(Lifecycle=Base, WindowsAdapter=object, SafetyError=lifecycle.SafetyError))
    with pytest.raises(lifecycle.SafetyError) as captured:
        Observed().transition("install_previous")
    assert captured.value is error
    value = candidate.failure_projection("msi", "installed_lifecycle", error, collector.DIAGNOSTICS)
    assert value["lifecycle_guard"]["guard_code"] == "UNKNOWN" and secret not in json.dumps(value)
    value = candidate.failure_projection("msi", "installed_lifecycle", error, {"lifecycle_guard": {"guard_code": secret, "failed_stage": secret},
        "last_mutation": {"exit_code": secret, "timed_out": secret, "command": secret, "process_cleanup": {"forced_termination": secret}}})
    assert value["lifecycle_guard"] == {"guard_code": "UNKNOWN", "failed_stage": "unknown"}
    assert value["last_mutation"]["exit_code"] is None and secret not in json.dumps(value)
