"""Materialize an immutable accepted v16 artifact into generated paths only.

The artifact keeps repository-relative layout and an index of every file hash.
This does not rebuild, install, approve, or publish anything. Existing files are
never overwritten; missing or mismatched content blocks the import.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat

ROOT = Path(__file__).resolve().parents[1]
MAX_FILES = 20000
MAX_TOTAL_BYTES = 8 * 1024**3


def allowed_path(name: object) -> Path:
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise ValueError("artifact path must be a canonical relative path")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise ValueError("artifact path escapes its generated destination")
    permitted = (
        name.startswith("desktop/src-tauri/target/release/")
        or name.startswith("build/v1600-evidence/accepted/")
        or name in {"build/v1600-evidence/nsis-installer-smoke.json", "build/v1600-evidence/msi-installer-smoke.json",
                    "build/generated/build-info.json", "dist/release/agent-sbom.cdx.json", "dist/release/THIRD_PARTY_NOTICES.txt"}
        or (name.startswith("build/upgrade-baseline/") and path.suffix.lower() in {".exe", ".msi"})
    )
    if not permitted:
        raise ValueError("artifact cannot write source, configuration or user-data paths")
    return path


def no_links(path: Path, boundary: Path, *, existing_only: bool = False) -> None:
    if not path.absolute().is_relative_to(boundary.absolute()):
        raise ValueError("artifact path is outside its boundary")
    for item in (path, *path.parents):
        if existing_only and not item.exists():
            continue
        info = item.lstat()
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("artifact links/reparse paths are forbidden")
        if item == boundary:
            break


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def plan(directory: Path, root: Path, expected_commit: str) -> list[tuple[Path, Path, str]]:
    directory = directory.absolute()
    no_links(directory, root, existing_only=True)
    index = directory / "artifact-index.json"
    no_links(index, directory)
    if index.stat().st_size > 8 * 1024**2:
        raise ValueError("artifact index is too large")
    data = json.loads(index.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("target_version") != "16.0.0"
            or data.get("source_commit") != expected_commit or data.get("accepted_candidate") is not True):
        raise ValueError("accepted artifact source/version contract does not match")
    files = data.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ValueError("accepted artifact file list is missing or exceeds its bound")
    result, seen, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise ValueError("artifact index entry is invalid")
        relative = allowed_path(item["path"])
        key = relative.as_posix().casefold()
        if key in seen:
            raise ValueError("artifact has repeated Windows paths")
        seen.add(key)
        source, target = directory / relative, root / relative
        no_links(source, directory)
        no_links(target.parent, root, existing_only=True)
        metadata = source.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or type(item["bytes"]) is not int or item["bytes"] != metadata.st_size
                or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in item["sha256"])):
            raise ValueError("artifact file identity is invalid")
        total += metadata.st_size
        if total > MAX_TOTAL_BYTES or digest(source) != item["sha256"]:
            raise ValueError("artifact byte/hash limits do not match")
        if target.exists():
            no_links(target, root)
            if not target.is_file() or digest(target) != item["sha256"]:
                raise ValueError("refusing to replace an existing generated artifact with different bytes")
        result.append((source, target, item["sha256"]))
    required = {"build/v1600-evidence/accepted/rc-bundle.json", "build/v1600-evidence/accepted/default-model-identity.json",
                "build/v1600-evidence/nsis-installer-smoke.json", "build/v1600-evidence/msi-installer-smoke.json"}
    if not required <= seen:
        raise ValueError("accepted artifact lacks the total RC/installer evidence")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        entries = plan(args.directory, ROOT, args.commit)
        if not args.dry_run:
            for source, target, expected in entries:
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                no_links(target.parent, ROOT)
                with source.open("rb") as origin, target.open("xb") as destination:
                    shutil.copyfileobj(origin, destination, length=1024 * 1024)
                if digest(target) != expected:
                    raise ValueError("materialized artifact hash changed")
        print(json.dumps({"status": "VALIDATED" if args.dry_run else "IMPORTED", "files": len(entries), "rebuilt": False, "published": False}))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "BLOCKED", "detail": "Accepted artifact is missing, unsafe or does not match immutable content"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
