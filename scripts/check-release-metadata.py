from __future__ import annotations

import ast
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V14_EVIDENCE_RUNNER = ROOT / "scripts" / "v14-evidence-runner.py"


def _v14_generated_evidence_workspace_clean() -> bool:
    """Apply the exact v14 generated-evidence exclusion policy.

    The evidence generator mirrors four machine-readable/human-readable files
    into ``docs/14.0.0`` and stores raw runs below ``build/v1400-evidence``.
    Those files are deliberately excluded from the v14 source fingerprint, so
    a release metadata gate must not call raw ``git status`` and contradict
    that identity.  Loading the runner rather than duplicating its porcelain
    parsing keeps both gates on one explicit exclusion contract; every other
    tracked or untracked change remains dirty.
    """

    try:
        specification = importlib.util.spec_from_file_location(
            "v14_evidence_runner_release_metadata", V14_EVIDENCE_RUNNER
        )
        if specification is None or specification.loader is None:
            return False
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module.source_workspace_clean(ROOT) is True
    except (OSError, ValueError, RuntimeError, ImportError):
        return False


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _toml(path: str) -> dict:
    with (ROOT / path).open("rb") as handle:
        return tomllib.load(handle)


def _python_version() -> str:
    tree = ast.parse((ROOT / "siyi/app/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            return str(ast.literal_eval(node.value))
    raise RuntimeError("siyi/app/__init__.py does not define __version__")


def _locked_package_version(path: str, package_name: str) -> str:
    lock = _toml(path)
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise RuntimeError(f"{path} does not define a package list")
    matches = [item for item in packages if item.get("name") == package_name]
    if len(matches) != 1:
        raise RuntimeError(
            f"{path} must define exactly one {package_name!r} package entry; "
            f"found {len(matches)}"
        )
    version = matches[0].get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(f"{path} package {package_name!r} has no valid version")
    return version


def machine_versions() -> dict[str, str]:
    backend_project = _toml("siyi/pyproject.toml")["project"]
    frontend_lock = _json("desktop/frontend/package-lock.json")
    cargo_lock = _toml("desktop/src-tauri/Cargo.lock")
    app_lock = next(item for item in cargo_lock["package"] if item["name"] == "app")
    return {
        "backend package": str(backend_project["version"]),
        "backend runtime": _python_version(),
        "backend lock": _locked_package_version(
            "siyi/uv.lock", str(backend_project["name"])
        ),
        "frontend package": str(_json("desktop/frontend/package.json")["version"]),
        "frontend lock root": str(frontend_lock["version"]),
        "frontend lock": str(frontend_lock["packages"][""]["version"]),
        "Tauri config": str(_json("desktop/src-tauri/tauri.conf.json")["version"]),
        "Cargo package": str(_toml("desktop/src-tauri/Cargo.toml")["package"]["version"]),
        "Cargo lock": str(app_lock["version"]),
    }


def _match(path: str, pattern: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"unable to read version from {path}")
    return match.group(1)


def user_visible_versions(expected: str) -> dict[str, str]:
    return {
        "README current version": _match("README.md", r"^当前版本：`([^`]+)`"),
        "current architecture": _match(
            "docs/CURRENT_ARCHITECTURE.md", r"^司忆 `([^`]+)`"
        ),
        "release notes": _match(
            f"docs/{expected}/RELEASE_NOTES.md", r"^# 司忆 v([^\s]+)"
        ),
    }


def evidence_versions() -> tuple[dict[str, str], dict[str, object]]:
    expected = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    release_root = f"docs/{expected}"
    status = _json(f"{release_root}/RELEASE_STATUS.json")
    evidence = _json(f"{release_root}/EVIDENCE_MANIFEST.json")
    matrix = _json(f"{release_root}/TEST_MATRIX.json")
    target_versions = {
        str(status.get("target_version")),
        str(evidence.get("target_version")),
        str(matrix.get("target_version")),
    }
    if len(target_versions) != 1:
        raise RuntimeError(f"v{expected} target version mismatch across status, evidence, and matrix")
    source_commits = {
        str(status.get("source_commit") or ""),
        str(evidence.get("source_commit") or ""),
        str(matrix.get("source_commit") or ""),
    }
    if len(source_commits) != 1:
        raise RuntimeError(f"v{expected} source commit mismatch across status, evidence, and matrix")
    commit = str(matrix.get("source_commit") or "")
    if status.get("test_status") == "READY" and not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"ready v{expected} test matrix must bind a full Git commit")
    return {
        "release status source": str(status.get("source_version")),
        "evidence manifest source": str(evidence.get("source_version")),
        "test matrix source": str(matrix.get("source_version")),
    }, status


def collected_versions(expected: str) -> tuple[dict[str, dict[str, str]], dict[str, object]]:
    evidence, status = evidence_versions()
    return {
        "machine_version_sources": machine_versions(),
        "user_visible_version_sources": user_visible_versions(expected),
        "evidence_version_sources": evidence,
    }, status


def _release_checks(expected: str, status: dict[str, object]) -> list[str]:
    errors: list[str] = []
    if not _v14_generated_evidence_workspace_clean():
        errors.append("official release metadata requires a clean worktree")
    required_status = {
        "implementation_status": "COMPLETE",
        "test_status": "READY",
        "distribution_status": "READY",
    }
    for field, required in required_status.items():
        if status.get(field) != required:
            errors.append(f"{field} must be {required}, found {status.get(field)}")
    source_commit = str(status.get("source_commit") or "")
    try:
        head = _git_head()
    except (OSError, subprocess.SubprocessError):
        errors.append("unable to resolve current Git HEAD")
    else:
        if source_commit != head:
            errors.append(
                f"release evidence source_commit must equal current Git HEAD; "
                f"found {source_commit or '<missing>'}, HEAD is {head}"
            )
    ref_name = os.environ.get("GITHUB_REF_NAME")
    if ref_name and ref_name != f"v{expected}":
        errors.append(f"tag {ref_name} does not match VERSION={expected}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate release truth sources")
    parser.add_argument(
        "--release",
        action="store_true",
        help="also require a clean tree and release-ready three-state status",
    )
    arguments = parser.parse_args()
    expected = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    categories, status = collected_versions(expected)
    mismatches = {
        f"{category}.{name}": value
        for category, versions in categories.items()
        for name, value in versions.items()
        if value != expected
    }
    release_errors = _release_checks(expected, status) if arguments.release else []
    if mismatches or release_errors:
        print(f"Release metadata does not match VERSION={expected}:", file=sys.stderr)
        for name, value in mismatches.items():
            print(f"- {name}: {value}", file=sys.stderr)
        for error in release_errors:
            print(f"- release_gate: {error}", file=sys.stderr)
        return 1
    for category, versions in categories.items():
        print(f"{category}: {len(versions)} sources consistent with {expected}")
    total = sum(len(versions) for versions in categories.values())
    print(f"Release metadata is consistent: {expected} ({total} sources checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
