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
GENERATED_EVIDENCE_FILENAMES = frozenset(
    {
        "TEST_MATRIX.json",
        "RELEASE_STATUS.json",
        "EVIDENCE_MANIFEST.json",
        "IMPLEMENTATION_FEEDBACK.md",
        "MODEL_BENCHMARK_REPORT.md",
        "MODEL_BENCHMARK.json",
    }
)
REQUIRED_RELEASE_DOCUMENT_FILENAMES = GENERATED_EVIDENCE_FILENAMES | {
    "RELEASE_NOTES.md"
}


def _compact_version(version: str) -> str:
    compact = re.sub(r"[^0-9A-Za-z]", "", version)
    if not compact:
        raise ValueError(f"version has no compact form: {version!r}")
    return compact


def _is_generated_evidence_path(relative_path: str, expected: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    evidence_prefix = f"build/v{_compact_version(expected)}-evidence"
    generated_documents = {
        f"docs/{expected}/{filename}" for filename in GENERATED_EVIDENCE_FILENAMES
    }
    return (
        normalized in generated_documents
        or normalized == evidence_prefix
        or normalized.startswith(evidence_prefix + "/")
    )


def _generated_evidence_workspace_clean(expected: str) -> bool:
    """Allow only current-version generated evidence beside a clean source tree."""

    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    entries = [entry for entry in completed.stdout.split(b"\0") if entry]
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            return False
        status_code = entry[:2]
        paths = [entry[3:]]
        if status_code[:1] in {b"R", b"C"} or status_code[1:2] in {b"R", b"C"}:
            if index >= len(entries):
                return False
            paths.append(entries[index])
            index += 1
        for raw_path in paths:
            relative_path = raw_path.decode("utf-8", errors="surrogateescape")
            if not _is_generated_evidence_path(relative_path, expected):
                return False
    return True


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


def _require_release_documents(expected: str) -> None:
    release_root = ROOT / "docs" / expected
    missing = sorted(
        filename
        for filename in REQUIRED_RELEASE_DOCUMENT_FILENAMES
        if not (release_root / filename).is_file()
    )
    if missing:
        raise RuntimeError(
            f"docs/{expected} is missing required release documents: "
            + ", ".join(missing)
        )


def evidence_versions() -> tuple[dict[str, str], dict[str, object]]:
    expected = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    release_root = f"docs/{expected}"
    status = _json(f"{release_root}/RELEASE_STATUS.json")
    evidence = _json(f"{release_root}/EVIDENCE_MANIFEST.json")
    matrix = _json(f"{release_root}/TEST_MATRIX.json")
    documents = {
        "RELEASE_STATUS.json": status,
        "EVIDENCE_MANIFEST.json": evidence,
        "TEST_MATRIX.json": matrix,
    }
    for filename, document in documents.items():
        for field in ("target_version", "source_version"):
            value = str(document.get(field))
            if value != expected:
                raise RuntimeError(
                    f"{filename} {field} must match VERSION={expected}; found {value}"
                )
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
    _require_release_documents(expected)
    evidence, status = evidence_versions()
    return {
        "machine_version_sources": machine_versions(),
        "user_visible_version_sources": user_visible_versions(expected),
        "evidence_version_sources": evidence,
    }, status


def _release_tag(expected: str) -> str | None:
    ref_type = os.environ.get("GITHUB_REF_TYPE")
    ref_name = os.environ.get("GITHUB_REF_NAME")
    if ref_type or ref_name:
        return ref_name if ref_type == "tag" else None
    try:
        completed = subprocess.run(
            ["git", "tag", "--points-at", "HEAD", "--list", f"v{expected}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tags = [tag.strip() for tag in completed.stdout.splitlines() if tag.strip()]
    return tags[0] if len(tags) == 1 else None


def _evidence_only_descendant(expected: str, source_commit: str, head: str) -> bool:
    """Accept a tested source followed only by current-version evidence files.

    Generated reports cannot contain the hash of the commit that contains the
    reports themselves.  The release tag may therefore point at a later
    evidence-only commit, but no executable or configuration path may change
    after the tested source commit.
    """

    if source_commit == head:
        return True
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        return False
    try:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_commit, head],
            cwd=ROOT,
            capture_output=True,
        )
        if ancestor.returncode != 0:
            return False
        changed = subprocess.run(
            ["git", "diff", "--name-only", "-z", source_commit, head, "--"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in changed.split(b"\0")
        if item
    ]
    return bool(paths) and all(_is_generated_evidence_path(path, expected) for path in paths)


def _release_preflight_checks(expected: str, *, require_tag: bool = False) -> list[str]:
    """Validate immutable release source without requiring generated reports."""

    errors: list[str] = []
    if not _generated_evidence_workspace_clean(expected):
        errors.append("official release metadata requires a clean worktree")
    if require_tag:
        release_tag = _release_tag(expected)
        if release_tag != f"v{expected}":
            errors.append(
                f"release must run from tag v{expected}; found {release_tag or '<no exact tag>'}"
            )
    return errors


def _release_checks(
    expected: str,
    status: dict[str, object],
    *,
    require_tag: bool = False,
) -> list[str]:
    errors = _release_preflight_checks(expected, require_tag=require_tag)
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
        if not _evidence_only_descendant(expected, source_commit, head):
            errors.append(
                "release evidence source_commit must identify current HEAD or its evidence-only source; "
                f"found {source_commit or '<missing>'}, HEAD is {head}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate release truth sources")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--release",
        action="store_true",
        help="also require a clean tree and release-ready three-state status",
    )
    modes.add_argument(
        "--release-preflight",
        action="store_true",
        help="validate clean tagged source before generated release evidence exists",
    )
    arguments = parser.parse_args()
    expected = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    if arguments.release_preflight:
        categories = {
            "machine_version_sources": machine_versions(),
            "user_visible_version_sources": user_visible_versions(expected),
        }
        status: dict[str, object] = {}
    else:
        categories, status = collected_versions(expected)
    mismatches = {
        f"{category}.{name}": value
        for category, versions in categories.items()
        for name, value in versions.items()
        if value != expected
    }
    if arguments.release:
        release_errors = _release_checks(expected, status, require_tag=True)
    elif arguments.release_preflight:
        release_errors = _release_preflight_checks(expected, require_tag=True)
    else:
        release_errors = []
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
