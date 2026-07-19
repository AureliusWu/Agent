from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "data",
    "__pycache__",
    ".pytest_cache",
}


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


def _source_files(root: Path) -> Iterable[Path]:
    listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for raw in listed.split("\0"):
        if not raw:
            continue
        relative = Path(raw)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        path = root / relative
        if path.is_file():
            yield relative


def source_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in sorted(_source_files(root), key=lambda item: item.as_posix().casefold()):
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _schema_version(root: Path) -> int:
    source = (root / "siyi" / "app" / "database.py").read_text(encoding="utf-8")
    match = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", source, re.MULTILINE)
    if not match:
        raise RuntimeError("Unable to locate SCHEMA_VERSION")
    return int(match.group(1))


def generate_manifest(root: Path, build_type: str, *, built_at: str | None = None) -> dict[str, object]:
    root = root.resolve()
    full_commit = _git(root, "rev-parse", "HEAD")
    short_commit = _git(root, "rev-parse", "--short=12", "HEAD")
    branch = _git(root, "branch", "--show-current") or "detached"
    workspace_state = "DIRTY" if _git(root, "status", "--porcelain=v1", "--untracked-files=all") else "CLEAN"
    fingerprint = source_fingerprint(root)
    timestamp = built_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    version = (root / "VERSION").read_text(encoding="ascii").strip()
    identity_material = "\n".join((version, full_commit, fingerprint, timestamp, build_type))
    build_id = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:24]
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
