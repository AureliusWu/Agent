from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-release-metadata.py"
SPEC = importlib.util.spec_from_file_location("check_release_metadata", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text("13.0.0\n", encoding="ascii")
    (root / "README.md").write_text("controlled source\n", encoding="utf-8")
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "release-test@example.invalid"],
        ["git", "config", "user.name", "Release Test"],
        ["git", "add", "VERSION", "README.md"],
        ["git", "commit", "-m", "release metadata fixture"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return root


def ready_status() -> dict[str, object]:
    return {
        "implementation_status": "COMPLETE",
        "test_status": "READY",
        "distribution_status": "READY",
        "source_commit": MODULE._git_head(),
    }


def write_machine_metadata(
    root: Path,
    *,
    version: str = "13.0.0",
    uv_version: str | None = None,
    frontend_lock_root_version: str | None = None,
    frontend_lock_package_version: str | None = None,
) -> None:
    uv_version = uv_version or version
    frontend_lock_root_version = frontend_lock_root_version or version
    frontend_lock_package_version = frontend_lock_package_version or version

    (root / "siyi" / "app").mkdir(parents=True)
    (root / "siyi" / "pyproject.toml").write_text(
        '[project]\nname = "aureliuswu-agent-backend"\n'
        f'version = "{version}"\n',
        encoding="utf-8",
    )
    (root / "siyi" / "app" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "siyi" / "uv.lock").write_text(
        "version = 1\n\n[[package]]\n"
        'name = "aureliuswu-agent-backend"\n'
        f'version = "{uv_version}"\n',
        encoding="utf-8",
    )

    frontend = root / "desktop" / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "package.json").write_text(
        json.dumps({"name": "agent-frontend", "version": version}), encoding="utf-8"
    )
    (frontend / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "agent-frontend",
                "version": frontend_lock_root_version,
                "packages": {
                    "": {
                        "name": "agent-frontend",
                        "version": frontend_lock_package_version,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    tauri = root / "desktop" / "src-tauri"
    tauri.mkdir(parents=True)
    (tauri / "tauri.conf.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    (tauri / "Cargo.toml").write_text(
        f'[package]\nname = "app"\nversion = "{version}"\n', encoding="utf-8"
    )
    (tauri / "Cargo.lock").write_text(
        f'[[package]]\nname = "app"\nversion = "{version}"\n', encoding="utf-8"
    )


def test_machine_versions_checks_uv_and_both_frontend_lock_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write_machine_metadata(root)
    monkeypatch.setattr(MODULE, "ROOT", root)

    versions = MODULE.machine_versions()

    assert versions["backend lock"] == "13.0.0"
    assert versions["frontend lock root"] == "13.0.0"
    assert versions["frontend lock"] == "13.0.0"
    assert len(versions) == 9


def test_machine_versions_exposes_uv_project_version_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write_machine_metadata(root, uv_version="12.9.9")
    monkeypatch.setattr(MODULE, "ROOT", root)

    versions = MODULE.machine_versions()

    assert versions["backend package"] == "13.0.0"
    assert versions["backend lock"] == "12.9.9"


def test_machine_versions_exposes_frontend_lock_root_mismatch_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write_machine_metadata(root, frontend_lock_root_version="12.9.9")
    monkeypatch.setattr(MODULE, "ROOT", root)

    versions = MODULE.machine_versions()

    assert versions["frontend lock root"] == "12.9.9"
    assert versions["frontend lock"] == "13.0.0"


def test_machine_versions_rejects_duplicate_uv_project_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    write_machine_metadata(root)
    uv_lock = root / "siyi" / "uv.lock"
    uv_lock.write_text(
        uv_lock.read_text(encoding="utf-8")
        + '\n[[package]]\nname = "aureliuswu-agent-backend"\nversion = "13.0.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE, "ROOT", root)

    with pytest.raises(RuntimeError, match="must define exactly one"):
        MODULE.machine_versions()


def test_release_metadata_uses_the_current_version_generated_evidence_exclusion_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    docs = root / "docs" / "14.0.0"
    docs.mkdir(parents=True)
    for filename in (
        "TEST_MATRIX.json",
        "RELEASE_STATUS.json",
        "EVIDENCE_MANIFEST.json",
        "IMPLEMENTATION_FEEDBACK.md",
    ):
        (docs / filename).write_text("generated mirror\n", encoding="utf-8")
    raw = root / "build" / "v1400-evidence" / "raw" / "a20.json"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"status":"PASS"}\n', encoding="utf-8")

    assert MODULE._release_checks("14.0.0", ready_status()) == []


def test_release_metadata_does_not_allow_a_previous_versions_generated_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    previous = root / "docs" / "12.0.0" / "TEST_MATRIX.json"
    previous.parent.mkdir(parents=True)
    previous.write_text("stale generated mirror\n", encoding="utf-8")

    errors = MODULE._release_checks("13.0.0", ready_status())

    assert "official release metadata requires a clean worktree" in errors


def test_release_metadata_requires_an_exact_version_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    monkeypatch.delenv("GITHUB_REF_TYPE", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)

    errors = MODULE._release_checks("13.0.0", ready_status(), require_tag=True)
    assert any("must run from tag v13.0.0" in error for error in errors)

    subprocess.run(
        ["git", "tag", "v13.0.0"], cwd=root, check=True, capture_output=True
    )
    assert MODULE._release_checks("13.0.0", ready_status(), require_tag=True) == []


def test_release_metadata_still_rejects_any_non_generated_dirty_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    generated = root / "docs" / "14.0.0" / "TEST_MATRIX.json"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated mirror\n", encoding="utf-8")
    (root / "docs" / "14.0.0" / "LOCAL_VOICE_INPUT.md").write_text(
        "not a generated evidence mirror\n", encoding="utf-8"
    )

    errors = MODULE._release_checks("14.0.0", ready_status())

    assert "official release metadata requires a clean worktree" in errors


def test_release_metadata_rejects_ready_evidence_from_a_different_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(MODULE, "ROOT", root)
    status = ready_status()
    status["source_commit"] = "0" * 40

    errors = MODULE._release_checks("14.0.0", status)

    assert any("source_commit must equal current Git HEAD" in error for error in errors)
