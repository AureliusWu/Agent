from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


V14_EVIDENCE_RUNNER = Path(__file__).with_name("v14-evidence-runner.py")
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


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _git_bytes(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


def _compact_version(version: str) -> str:
    compact = re.sub(r"[^0-9A-Za-z]", "", version)
    if not compact:
        raise RuntimeError(f"Version has no safe evidence-directory form: {version!r}")
    return compact


def _is_current_generated_evidence(relative_path: str, version: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    evidence_prefix = f"build/v{_compact_version(version)}-evidence"
    return (
        normalized in {
            f"docs/{version}/{filename}" for filename in GENERATED_EVIDENCE_FILENAMES
        }
        or normalized == evidence_prefix
        or normalized.startswith(evidence_prefix + "/")
    )


def _dynamic_workspace_clean(root: Path, version: str) -> bool:
    entries = [
        entry
        for entry in _git_bytes(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        ).split(b"\0")
        if entry
    ]
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            raise RuntimeError("Git returned an invalid porcelain status entry")
        status_code = entry[:2]
        paths = [entry[3:]]
        if status_code[:1] in {b"R", b"C"} or status_code[1:2] in {b"R", b"C"}:
            if index >= len(entries):
                raise RuntimeError("Git returned an incomplete rename/copy status entry")
            paths.append(entries[index])
            index += 1
        for raw_path in paths:
            relative_path = raw_path.decode("utf-8", errors="surrogateescape")
            if not _is_current_generated_evidence(relative_path, version):
                return False
    return True


def _dynamic_source_fingerprint(root: Path, version: str) -> str:
    tracked = _git_bytes(root, "ls-files", "-z")
    untracked = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    paths = sorted({path for path in tracked.split(b"\0") + untracked.split(b"\0") if path})
    digest = hashlib.sha256()
    digest.update(b"siyi-release-source-tree-fingerprint-v2\0")
    digest.update(version.encode("ascii"))
    digest.update(b"\0")
    resolved_root = root.resolve()
    for raw_path in paths:
        relative_path = raw_path.decode("utf-8", errors="surrogateescape")
        if _is_current_generated_evidence(relative_path, version):
            continue
        candidate = root / relative_path
        try:
            candidate.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise RuntimeError(
                f"Source fingerprint path escapes repository root: {relative_path}"
            ) from exc
        digest.update(len(raw_path).to_bytes(8, "big"))
        digest.update(raw_path)
        if candidate.is_symlink():
            payload = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
            kind = b"L"
        elif candidate.is_file():
            payload = candidate.read_bytes()
            kind = b"F"
        elif not candidate.exists():
            payload = b""
            kind = b"D"
        else:
            raise RuntimeError(f"Unsupported source entry: {relative_path}")
        digest.update(kind)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest().upper()


def _release_source_identity(root: Path) -> dict[str, object]:
    """Build a VERSION-scoped identity; preserve the historical v14 hash contract."""

    version = (root / "VERSION").read_text(encoding="ascii").strip()
    if version == "14.0.0" and V14_EVIDENCE_RUNNER.is_file():
        specification = importlib.util.spec_from_file_location(
            "v14_evidence_runner_build_info", V14_EVIDENCE_RUNNER
        )
        if specification is None or specification.loader is None:
            raise RuntimeError("Unable to load the v14 release identity compatibility module")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        identity = module.source_identity(root)
        if not isinstance(identity, dict):
            raise RuntimeError("The v14 release identity compatibility module returned invalid data")
        return identity
    return {
        "source_version": version,
        "source_commit": _git(root, "rev-parse", "HEAD"),
        "workspace_clean": _dynamic_workspace_clean(root, version),
        "source_tree_fingerprint": _dynamic_source_fingerprint(root, version),
    }


def _schema_version(root: Path) -> int:
    source = (root / "siyi" / "app" / "database.py").read_text(encoding="utf-8")
    match = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", source, re.MULTILINE)
    if not match:
        raise RuntimeError("Unable to locate SCHEMA_VERSION")
    return int(match.group(1))


def _release_truth(root: Path, product_version: str) -> tuple[dict[str, str], str]:
    candidates: list[tuple[tuple[int, int, int], Path]] = []
    for path in (root / "docs").glob("*/RELEASE_STATUS.json"):
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", path.parent.name)
        if match:
            candidates.append((tuple(int(item) for item in match.groups()), path))
    for _, path in sorted(candidates, reverse=True):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("source_version") != product_version:
            continue
        status = {
            key: str(payload[key])
            for key in (
                "target_version",
                "source_version",
                "implementation_status",
                "test_status",
                "distribution_status",
            )
        }
        evidence_path = path.with_name("EVIDENCE_MANIFEST.json")
        evidence_hash = (
            hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            if evidence_path.is_file()
            else "unavailable"
        )
        return status, evidence_hash
    return {
        "target_version": product_version,
        "source_version": product_version,
        "implementation_status": "UNKNOWN",
        "test_status": "NOT_READY",
        "distribution_status": "NOT_DISTRIBUTED",
    }, "unavailable"


def generate_manifest(root: Path, build_type: str, *, built_at: str | None = None) -> dict[str, object]:
    root = root.resolve()
    full_commit = _git(root, "rev-parse", "HEAD")
    short_commit = _git(root, "rev-parse", "--short=12", "HEAD")
    branch = _git(root, "branch", "--show-current") or "detached"
    release_identity = _release_source_identity(root)
    workspace_state = "CLEAN" if release_identity.get("workspace_clean") is True else "DIRTY"
    fingerprint = str(release_identity["source_tree_fingerprint"])
    timestamp = built_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    version = (root / "VERSION").read_text(encoding="ascii").strip()
    # Build identity describes source/product identity. The wall-clock timestamp
    # remains metadata and must not make identical source generate a different
    # component ID during validation or repackaging.
    identity_material = "\n".join((version, full_commit, fingerprint, build_type))
    build_id = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:24]
    release_status, evidence_manifest_hash = _release_truth(root, version)
    return {
        "manifest_version": 1,
        "product_version": version,
        "git_commit": full_commit,
        "git_short_commit": short_commit,
        "git_branch": branch,
        "build_time": timestamp,
        "build_type": build_type,
        "workspace_state": workspace_state,
        "source_fingerprint": fingerprint,
        "build_id": build_id,
        "component_build_ids": {
            "tauri": f"tauri-{build_id}",
            "react": f"react-{build_id}",
            "sidecar": f"sidecar-{build_id}",
        },
        "database_schema_version": _schema_version(root),
        "release_status": release_status,
        "evidence_manifest_hash": evidence_manifest_hash,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the immutable Siyi build manifest.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--build-type", choices=("Development", "Release"), default="Release")
    parser.add_argument("--respect-lock", action="store_true")
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    output = arguments.output or Path(os.environ.get("SIYI_BUILD_MANIFEST", root / "build" / "generated" / "build-info.json"))
    output = output.resolve()
    if arguments.respect_lock and os.environ.get("SIYI_BUILD_INFO_LOCKED") == "1" and output.is_file():
        print(output)
        return 0
    manifest = generate_manifest(root, arguments.build_type)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
