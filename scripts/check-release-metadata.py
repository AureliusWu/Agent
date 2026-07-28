from __future__ import annotations

import ast
import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


def machine_versions() -> dict[str, str]:
    cargo_lock = _toml("desktop/src-tauri/Cargo.lock")
    app_lock = next(item for item in cargo_lock["package"] if item["name"] == "app")
    return {
        "backend package": str(_toml("siyi/pyproject.toml")["project"]["version"]),
        "backend runtime": _python_version(),
        "frontend package": str(_json("desktop/frontend/package.json")["version"]),
        "frontend lock": str(_json("desktop/frontend/package-lock.json")["packages"][""]["version"]),
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
    workspace = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    if workspace:
        errors.append("official release metadata requires a clean worktree")
    required_status = {
        "implementation_status": "COMPLETE",
        "test_status": "READY",
        "distribution_status": "READY",
    }
    for field, required in required_status.items():
        if status.get(field) != required:
            errors.append(f"{field} must be {required}, found {status.get(field)}")
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
