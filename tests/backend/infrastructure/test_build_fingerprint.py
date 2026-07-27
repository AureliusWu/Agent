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


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "siyi" / "app").mkdir(parents=True)
    (root / "VERSION").write_text("2.0.1\n", encoding="ascii")
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


def test_generated_v8_evidence_does_not_change_product_identity(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    evidence = root / "docs" / "8.0.0" / "TEST_MATRIX.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("first report\n", encoding="utf-8")
    first = MODULE.generate_manifest(root, "Release")
    evidence.write_text("updated evidence\n", encoding="utf-8")
    second = MODULE.generate_manifest(root, "Release")

    assert first["source_fingerprint"] == second["source_fingerprint"]
    assert first["build_id"] == second["build_id"]
