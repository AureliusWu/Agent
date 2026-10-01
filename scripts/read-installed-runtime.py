"""Read-only identity of the actual installed executables and onedir payload."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import stat

from rc_payload_inventory import inventory

ROOT = Path(__file__).resolve().parents[1]


def checked_file(root: Path, name: str) -> Path:
    if not name or Path(name).name != name or "/" in name or "\\" in name or Path(name).suffix.lower() != ".exe":
        raise ValueError("only a local executable filename is accepted")
    path = root / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("installed executable must be an ordinary file")
    return path


def digest(path: Path) -> str:
    before = path.stat()
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("installed executable changed while it was read")
    return value.hexdigest()


def observe(directory: Path, desktop: str, sidecar: str) -> dict:
    root = directory.absolute()
    for path in (root, *root.parents):
        metadata = path.lstat()
        if not path.is_dir() or path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("installed directory ancestors must be ordinary directories")
    files = {"desktop": checked_file(root, desktop), "sidecar": checked_file(root, sidecar)}
    hashes = {key: digest(path) for key, path in files.items()}
    payload = inventory(root, {"path": sidecar, "sha256": hashes["sidecar"]})
    if any(digest(path) != hashes[key] for key, path in files.items()):
        raise ValueError("installed executables changed during payload inventory")
    return {"binary_sha256": hashes, "sidecar_payload_sha256": payload["payload_content_sha256"],
            "installed_sidecar_payload": payload}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--desktop")
    parser.add_argument("--sidecar", default="agent-backend.exe")
    parser.add_argument("--capture-source", action="store_true")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--build-id")
    args = parser.parse_args(argv)
    try:
        if args.source_only:
            if args.directory or args.desktop or args.capture_source or not re.fullmatch(r"[0-9a-f]{24}", args.build_id or ""):
                raise ValueError("source-only requires exactly the validated build ID")
            from generate_build_info import _release_source_identity
            print(json.dumps({**_release_source_identity(ROOT), "build_id": args.build_id}))
            return 0
        if args.directory is None or not args.desktop:
            raise ValueError("actual installed directory and executable are required")
        if args.capture_source:
            from generate_build_info import _release_source_identity
            before = _release_source_identity(ROOT)
        report = observe(args.directory, args.desktop, args.sidecar)
        if args.capture_source:
            after = _release_source_identity(ROOT)
            if before != after:
                raise ValueError("source changed during installed identity capture")
            manifest_path = args.directory / "_internal/build-info.json"
            if manifest_path.is_symlink() or manifest_path.stat().st_size > 64 * 1024:
                raise ValueError("installed manifest is not bounded ordinary metadata")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report["source_after"] = {**after, "build_id": manifest["build_id"]}
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "FAIL", "detail": "Actual installed runtime identity could not be safely captured"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
