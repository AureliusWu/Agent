"""Offline GitHub metadata fixtures; never produce accepted RC evidence."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("rc_run_provenance", ROOT / "scripts/validate-rc-run.py")
assert spec and spec.loader
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
REPOSITORY = "Owner/Repo"
COMMIT = "a" * 40


def valid_run():
    return {
        "id": 123, "run_attempt": 1, "workflow_id": 456,
        "repository": {"id": 42, "full_name": REPOSITORY},
        "head_repository": {"id": 42, "full_name": REPOSITORY},
        "head_sha": COMMIT, "head_branch": "main",
        "path": ".github/workflows/rc-acceptance.yml", "event": "workflow_dispatch",
        "status": "completed", "conclusion": "success",
        "run_started_at": "2026-11-05T01:00:00Z", "updated_at": "2026-11-05T01:10:00Z",
    }


def validate(run, **kwargs):
    return validator.validate_run(run, repository=REPOSITORY, commit=COMMIT, run_id=123, **kwargs)


def test_formal_main_acceptance_metadata_can_be_selected_and_rechecked():
    receipt = validate(valid_run())
    assert receipt["report_type"] == "rc_run_selection_not_acceptance"
    assert receipt["run_attempt"] == 1
    assert validate(valid_run(), expected_receipt=receipt) == receipt
    assert "passed" not in receipt and "rc_eligible" not in receipt


@pytest.mark.parametrize(("field", "value"), [
    ("path", ".github/workflows/rc-offline.yml"),
    ("path", ".github/workflows/rc-acceptance.yml@refs/heads/other"),
    ("head_branch", "other"), ("head_branch", "v16.0.1"),
    ("event", "push"), ("event", "pull_request"), ("event", "workflow_run"),
    ("status", "in_progress"), ("status", "queued"),
    ("conclusion", "failure"), ("conclusion", "cancelled"), ("conclusion", "skipped"),
    ("id", 124), ("id", "123"), ("id", True),
    ("head_sha", "b" * 40), ("run_attempt", 0), ("run_attempt", True),
    ("run_attempt", "1"), ("workflow_id", None),
])
def test_same_repository_and_sha_do_not_bypass_formal_source_checks(field, value):
    run = valid_run()
    run[field] = value
    with pytest.raises(ValueError):
        validate(run)


@pytest.mark.parametrize("field", list(valid_run()))
def test_missing_run_metadata_is_never_accepted(field):
    run = valid_run()
    del run[field]
    with pytest.raises(ValueError):
        validate(run)


@pytest.mark.parametrize("change", [
    {"id": 43, "full_name": "Fork/Repo"},
    {"id": 43, "full_name": REPOSITORY},
    {"id": 42, "full_name": "Fork/Repo"},
    {"full_name": REPOSITORY}, {"id": True, "full_name": REPOSITORY},
])
def test_fork_or_incomplete_head_repository_is_rejected(change):
    run = valid_run()
    run["head_repository"] = change
    with pytest.raises(ValueError):
        validate(run)


@pytest.mark.parametrize(("field", "value"), [("run_attempt", 2), ("workflow_id", 789)])
def test_download_recheck_rejects_attempt_or_workflow_identity_drift(field, value):
    receipt = validate(valid_run())
    run = valid_run()
    run[field] = value
    with pytest.raises(ValueError, match="changed during artifact download"):
        validate(run, expected_receipt=receipt)


@pytest.mark.parametrize("mutation", ["missing", "unknown", "float", "boolean"])
def test_incomplete_or_retyped_selection_receipt_cannot_skip_recheck(mutation):
    receipt = copy.deepcopy(validate(valid_run()))
    if mutation == "missing":
        del receipt["run_attempt"]
    elif mutation == "unknown":
        receipt["accepted"] = True
    elif mutation == "float":
        receipt["run_attempt"] = 1.0
    else:
        receipt["run_attempt"] = True
    with pytest.raises(ValueError):
        validate(valid_run(), expected_receipt=receipt)


def test_cli_writes_fresh_selection_receipt_and_rechecks_without_network(tmp_path, capsys):
    source, receipt = tmp_path / "run.json", tmp_path / "selection.json"
    source.write_text(json.dumps(valid_run()), encoding="utf-8")
    artifacts = tmp_path / "artifacts.json"
    artifacts.write_text(json.dumps({"total_count": 1, "artifacts": [valid_artifact()]}), encoding="utf-8")
    arguments = ["--run-json", str(source), "--attempt-json", str(source), "--repository", REPOSITORY,
                 "--commit", COMMIT, "--run-id", "123", "--artifacts-json", str(artifacts),
                 "--artifact-name", "Siyi-RC-Acceptance-v16.0.1"]
    assert validator.main(arguments + ["--write-receipt", str(receipt)]) == 0
    assert validator.main(arguments + ["--expected-receipt", str(receipt)]) == 0
    unchanged = receipt.read_bytes()
    assert validator.main(arguments + ["--write-receipt", str(receipt)]) == 1
    assert receipt.read_bytes() == unchanged
    assert "NOT_RC_ACCEPTED" in capsys.readouterr().out


@pytest.mark.parametrize("content", ['{"id":123,"id":123}', '{"id":NaN}'])
def test_ambiguous_json_metadata_is_rejected(tmp_path, content):
    path = tmp_path / "ambiguous.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        validator.read_metadata(path)


def test_large_metadata_is_rejected_before_json_parse(tmp_path, monkeypatch):
    path = tmp_path / "large.json"
    path.write_text(json.dumps(valid_run()), encoding="utf-8")
    monkeypatch.setattr(validator, "MAX_METADATA_BYTES", 1)
    with pytest.raises(ValueError, match="bounded reader"):
        validator.read_metadata(path)


def valid_artifact():
    return {"id": 789, "name": "Siyi-RC-Acceptance-v16.0.1", "expired": False,
            "size_in_bytes": 100, "digest": "sha256:" + "b" * 64,
            "created_at": "2026-11-05T01:09:00Z",
            "workflow_run": {"id": 123, "repository_id": 42, "head_repository_id": 42,
                             "head_sha": COMMIT, "head_branch": "main"}}


def select(artifacts, **kwargs):
    return validator.validate_selection(valid_run(), artifacts, artifact_name="Siyi-RC-Acceptance-v16.0.1",
                                        repository=REPOSITORY, commit=COMMIT, run_id=123, **kwargs)


def test_unique_immutable_artifact_can_be_selected_from_all_pages_and_rechecked_by_id():
    artifact = valid_artifact()
    log = copy.deepcopy(artifact)
    log.update(id=790, name="unrelated-build-log")
    receipt = select([{"total_count": 2, "artifacts": [log]}, {"total_count": 2, "artifacts": [artifact]}])
    assert receipt["artifact"]["id"] == 789
    assert select(artifact, expected_receipt=receipt) == receipt


@pytest.mark.parametrize(("field", "value"), [
    ("created_at", "2026-11-05T00:59:59Z"), ("created_at", "2026-11-05T01:10:01Z"),
    ("created_at", "2026-11-05T01:00:00Z"),
    ("created_at", "2026-11-05T01:09:00"), ("created_at", None),
    ("expired", True), ("expired", 0), ("digest", None), ("size_in_bytes", 0), ("id", True),
])
def test_old_attempt_or_unqualified_artifact_is_rejected(field, value):
    artifact = valid_artifact()
    artifact[field] = value
    with pytest.raises(ValueError):
        select(artifact)


@pytest.mark.parametrize(("field", "value"), [
    ("id", 124), ("repository_id", 43), ("head_repository_id", 43),
    ("head_sha", "c" * 40), ("head_branch", "other"),
])
def test_artifact_from_other_run_or_fork_is_rejected(field, value):
    artifact = valid_artifact()
    artifact["workflow_run"][field] = value
    with pytest.raises(ValueError):
        select(artifact)


def test_duplicate_name_and_truncated_or_duplicate_pages_fail_closed():
    artifact = valid_artifact()
    second = copy.deepcopy(artifact)
    second["id"] = 790
    for pages in [[{"total_count": 2, "artifacts": [artifact, second]}],
                  [{"total_count": 2, "artifacts": [artifact]}],
                  [{"total_count": 2, "artifacts": [artifact, artifact]}],
                  [{"total_count": 0, "artifacts": []}]]:
        with pytest.raises(ValueError):
            select(pages)


@pytest.mark.parametrize(("field", "value"), [("id", 790), ("digest", "sha256:" + "c" * 64),
                                              ("size_in_bytes", 101)])
def test_artifact_selection_cannot_drift_after_download(field, value):
    artifact = valid_artifact()
    receipt = select(artifact)
    artifact[field] = value
    with pytest.raises(ValueError, match="changed during download"):
        select(artifact, expected_receipt=receipt)


def test_artifact_retained_from_earlier_attempt_is_not_accepted_for_a_later_attempt():
    run = valid_run()
    run.update(run_attempt=2, run_started_at="2026-11-05T02:00:00Z", updated_at="2026-11-05T02:10:00Z")
    with pytest.raises(ValueError, match="selected run attempt"):
        validator.validate_selection(run, valid_artifact(), artifact_name="Siyi-RC-Acceptance-v16.0.1",
                                      repository=REPOSITORY, commit=COMMIT, run_id=123)


@pytest.mark.parametrize("field", list(valid_artifact()))
def test_missing_artifact_metadata_is_rejected(field):
    artifact = valid_artifact()
    del artifact[field]
    with pytest.raises(ValueError):
        select({"total_count": 1, "artifacts": [artifact]})


@pytest.mark.parametrize("suffix", ["@main", "@refs/heads/main"])
def test_official_main_workflow_path_alias_is_allowed(suffix):
    run = valid_run()
    run["path"] += suffix
    assert validate(run)["workflow_path"] == validator.WORKFLOW_PATH


@pytest.mark.parametrize("field,value", [("run_attempt", 2), ("run_started_at", "2026-11-05T00:00:00Z")])
def test_cli_rejects_latest_metadata_inconsistent_with_explicit_attempt(tmp_path, field, value):
    source, attempt, artifacts = (tmp_path / name for name in ["latest.json", "attempt.json", "artifacts.json"])
    source.write_text(json.dumps(valid_run()), encoding="utf-8")
    specific = valid_run()
    specific[field] = value
    attempt.write_text(json.dumps(specific), encoding="utf-8")
    artifacts.write_text(json.dumps(valid_artifact()), encoding="utf-8")
    assert validator.main(["--run-json", str(source), "--attempt-json", str(attempt),
                           "--artifacts-json", str(artifacts), "--artifact-name", "Siyi-RC-Acceptance-v16.0.1",
                           "--repository", REPOSITORY, "--commit", COMMIT, "--run-id", "123"]) == 1
