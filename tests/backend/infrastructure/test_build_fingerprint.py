from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "generate_build_info.py"
SPEC = importlib.util.spec_from_file_location("generate_build_info", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def repository(tmp_path: Path, *, version: str = "2.0.1") -> Path:
    root = tmp_path / "repo"
    (root / "siyi" / "app").mkdir(parents=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="ascii")
    (root / "siyi" / "app" / "database.py").write_text("SCHEMA_VERSION = 22\n", encoding="utf-8")
    (root / "source.txt").write_text("first\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "build-test@example.invalid")
    git(root, "config", "user.name", "Build Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    return root


def test_dirty_source_changes_fingerprint_without_changing_head(tmp_path: Path) -> None:
    root = repository(tmp_path)
    clean = MODULE.generate_manifest(root, "Release", built_at="2026-07-16T00:00:00+00:00")
    (root / "source.txt").write_text("second\n", encoding="utf-8")
    dirty = MODULE.generate_manifest(root, "Release", built_at="2026-07-16T00:00:00+00:00")

    assert clean["workspace_state"] == "CLEAN"
    assert dirty["workspace_state"] == "DIRTY"
    assert clean["git_commit"] == dirty["git_commit"]
    assert clean["source_fingerprint"] != dirty["source_fingerprint"]
    assert clean["build_id"] != dirty["build_id"]


def test_new_commit_changes_embedded_git_revision(tmp_path: Path) -> None:
    root = repository(tmp_path)
    previous = MODULE.generate_manifest(root, "Release", built_at="2026-07-16T00:00:00+00:00")
    (root / "source.txt").write_text("committed change\n", encoding="utf-8")
    git(root, "add", "source.txt")
    git(root, "commit", "-m", "change")
    current = MODULE.generate_manifest(root, "Release", built_at="2026-07-16T00:00:01+00:00")

    assert current["workspace_state"] == "CLEAN"
    assert current["git_commit"] != previous["git_commit"]
    assert current["git_short_commit"] != previous["git_short_commit"]
    assert current["build_id"] != previous["build_id"]


def test_build_time_does_not_change_source_identity(tmp_path: Path) -> None:
    root = repository(tmp_path)
    first = MODULE.generate_manifest(
        root, "Release", built_at="2026-07-16T00:00:00+00:00"
    )
    second = MODULE.generate_manifest(
        root, "Release", built_at="2026-07-16T00:01:00+00:00"
    )

    assert first["build_time"] != second["build_time"]
    assert first["source_fingerprint"] == second["source_fingerprint"]
    assert first["build_id"] == second["build_id"]


def test_generated_current_version_evidence_does_not_change_product_identity_or_clean_state(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    evidence = root / "docs" / "2.0.1" / "TEST_MATRIX.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("first report\n", encoding="utf-8")
    first = MODULE.generate_manifest(root, "Release")
    evidence.write_text("updated evidence\n", encoding="utf-8")
    second = MODULE.generate_manifest(root, "Release")

    assert first["workspace_state"] == "CLEAN"
    assert second["workspace_state"] == "CLEAN"
    assert first["source_fingerprint"] == second["source_fingerprint"]
    assert first["build_id"] == second["build_id"]


def test_non_generated_current_version_document_changes_product_identity(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    document = root / "docs" / "2.0.1" / "LOCAL_VOICE_INPUT.md"
    document.parent.mkdir(parents=True)
    document.write_text("source documentation\n", encoding="utf-8")

    manifest = MODULE.generate_manifest(root, "Release")

    assert manifest["workspace_state"] == "DIRTY"


def test_previous_version_evidence_does_not_bypass_current_release_identity(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    stale = root / "docs" / "2.0.0" / "TEST_MATRIX.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale evidence\n", encoding="utf-8")

    manifest = MODULE.generate_manifest(root, "Release")

    assert manifest["workspace_state"] == "DIRTY"


def test_v14_release_identity_remains_compatible_with_the_v14_runner(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path, version="14.0.0")
    evidence = root / "docs" / "14.0.0" / "TEST_MATRIX.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("v14 compatibility evidence\n", encoding="utf-8")

    manifest = MODULE.generate_manifest(root, "Release")

    assert manifest["workspace_state"] == "CLEAN"
    assert len(str(manifest["source_fingerprint"])) == 64


def test_build_manifest_embeds_release_truth_and_evidence_hash(tmp_path: Path) -> None:
    root = repository(tmp_path)
    release = root / "docs" / "2.0.2"
    release.mkdir(parents=True)
    (release / "RELEASE_STATUS.json").write_text(
        '{"target_version":"2.0.2","source_version":"2.0.1",'
        '"implementation_status":"PARTIAL","test_status":"NOT_READY",'
        '"distribution_status":"NOT_DISTRIBUTED"}',
        encoding="utf-8",
    )
    evidence = b'{"schema_version":1,"source_version":"2.0.1"}'
    (release / "EVIDENCE_MANIFEST.json").write_bytes(evidence)

    manifest = MODULE.generate_manifest(root, "Release")

    assert manifest["release_status"] == {
        "target_version": "2.0.2",
        "source_version": "2.0.1",
        "implementation_status": "PARTIAL",
        "test_status": "NOT_READY",
        "distribution_status": "NOT_DISTRIBUTED",
    }
    import hashlib

    assert manifest["evidence_manifest_hash"] == hashlib.sha256(evidence).hexdigest()
