from __future__ import annotations

"""Read a bounded ``build-info.json`` entry from a PyInstaller onefile archive.

This helper deliberately uses ``CArchiveReader`` only: it does not execute the
frozen application and never extracts archive entries to disk.  It emits a
small, stable summary instead of the complete embedded manifest so release
evidence can bind the archive entry without exposing unrelated metadata.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from PyInstaller.archive.readers import CArchiveReader


ENTRY_NAME = "build-info.json"
MAX_EMBEDDED_MANIFEST_BYTES = 128 * 1024


class ArchiveReadError(ValueError):
    """The frozen archive does not expose one safe embedded manifest."""


def extract_embedded_build_info(binary: Path) -> dict[str, object]:
    # Refuse a reparse/symlink input before resolution so an evidence caller
    # cannot make this reader follow an arbitrary target outside its owned
    # installer tree.
    if binary.is_symlink():
        raise ArchiveReadError("binary is not a regular file")
    resolved = binary.resolve(strict=True)
    if not resolved.is_file():
        raise ArchiveReadError("binary is not a regular file")
    archive = CArchiveReader(str(resolved))
    entries = [name for name in archive.toc if name == ENTRY_NAME]
    if len(entries) != 1:
        raise ArchiveReadError("archive must contain exactly one build-info entry")
    payload_bytes = archive.extract(ENTRY_NAME)
    if not isinstance(payload_bytes, bytes) or not payload_bytes:
        raise ArchiveReadError("embedded build-info entry is empty")
    if len(payload_bytes) > MAX_EMBEDDED_MANIFEST_BYTES:
        raise ArchiveReadError("embedded build-info entry exceeds the bounded size")
    try:
        manifest: Any = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveReadError("embedded build-info entry is not UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ArchiveReadError("embedded build-info entry is not an object")
    component_ids = manifest.get("component_build_ids")
    component_build_id = component_ids.get("sidecar") if isinstance(component_ids, dict) else None
    return {
        "archive_entry": ENTRY_NAME,
        "embedded_manifest_bytes": len(payload_bytes),
        "embedded_manifest_sha256": hashlib.sha256(payload_bytes).hexdigest().upper(),
        "product_version": manifest.get("product_version"),
        "git_commit": manifest.get("git_commit"),
        "source_fingerprint": manifest.get("source_fingerprint"),
        "workspace_state": manifest.get("workspace_state"),
        "build_id": manifest.get("build_id"),
        "component_build_id": component_build_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, help="Existing PyInstaller onefile executable")
    arguments = parser.parse_args(argv)
    try:
        summary = extract_embedded_build_info(arguments.binary)
    except (ArchiveReadError, OSError, ValueError):
        print("Could not safely read the embedded PyInstaller build manifest.", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
