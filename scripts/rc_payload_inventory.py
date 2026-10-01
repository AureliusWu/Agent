"""Bounded hashes for the complete PyInstaller onedir payload."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import time

MAX_FILES = 20000
MAX_BYTES = 4 * 1024 ** 3


def content_summary(value: dict) -> str:
    binary = value.get("binary", {})
    prefix = (Path(str(binary.get("path", ""))).parent / "_internal").as_posix() + "/"
    entries = value.get("entries")
    if (value.get("schema_version") != 1 or value.get("report_type") != "rc_sidecar_payload_inventory"
            or not isinstance(entries, list) or not entries or len(entries) > MAX_FILES):
        raise ValueError("complete sidecar payload inventory is required")
    normalized, paths, total = [], set(), 0
    for item in entries:
        path = item.get("path", "")
        size, digest = item.get("bytes"), item.get("sha256")
        if (not isinstance(path, str) or not path.startswith(prefix) or Path(path).is_absolute() or ".." in Path(path).parts
                or type(size) is not int or size < 0 or not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            raise ValueError("invalid sidecar payload entry")
        relative = path[len(prefix):]
        if not relative or relative.casefold() in paths:
            raise ValueError("duplicated sidecar payload file")
        paths.add(relative.casefold())
        total += size
        normalized.append({"path": relative, "bytes": size, "sha256": digest})
    if total > MAX_BYTES or value.get("file_count") != len(entries) or value.get("total_bytes") != total:
        raise ValueError("sidecar payload counts or byte summary disagree")
    normalized.sort(key=lambda item: item["path"])
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def inventory(root: Path, binary: dict[str, str]) -> dict:
    directory = root / Path(binary["path"]).parent / "_internal"
    if not directory.is_dir():
        raise ValueError("the frozen sidecar onedir payload is missing")
    for path in (directory, *directory.parents):
        if path == root:
            break
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("sidecar payload parent is a reparse point")
    started = time.monotonic()
    entries, total = [], 0
    pending, files, scanned = [directory], [], 0
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            scanned += 1
            info = path.lstat()
            if scanned > MAX_FILES or time.monotonic() - started > 120:
                raise ValueError("sidecar payload enumeration exceeded its bounds")
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("sidecar payload contains a reparse point")
            if path.is_dir():
                pending.append(path)
            else:
                files.append(path)
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("sidecar payload contains a reparse point")
        if path.is_dir():
            continue
        if not path.is_file() or len(entries) >= MAX_FILES or total + info.st_size > MAX_BYTES:
            raise ValueError("sidecar payload exceeds file/byte bounds or contains a special file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                if time.monotonic() - started > 120:
                    raise ValueError("sidecar payload hashing timed out")
                digest.update(block)
        after = path.stat()
        if (info.st_size, info.st_mtime_ns, info.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("sidecar payload changed while it was hashed")
        total += info.st_size
        entries.append({"path": path.relative_to(root).as_posix(), "bytes": info.st_size, "sha256": digest.hexdigest()})
    if not entries:
        raise ValueError("sidecar payload is empty")
    value = {"schema_version": 1, "report_type": "rc_sidecar_payload_inventory", "binary": binary,
             "file_count": len(entries), "total_bytes": total, "entries": entries}
    value["payload_content_sha256"] = content_summary(value)
    return value
