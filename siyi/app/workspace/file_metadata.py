"""Bounded encoding hints, never full-file text materialization for metadata."""
from __future__ import annotations

import codecs
import os
from pathlib import Path
from typing import Any

from app.workspace.file_recovery import RecoveryError

ENCODING_PROBE_BYTES = 64 * 1024


def encoding_hint(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state["type"] != "file":
        return {"encoding": None, "encoding_source": "not_applicable", "encoding_probe_bytes": 0, "text_read_within_limit": False}
    with path.open("rb") as stream:
        metadata = os.fstat(stream.fileno())
        expected = state["identity"]
        if (metadata.st_dev != expected["device"] or metadata.st_ino != expected["inode"]
                or metadata.st_mtime_ns != expected["mtime_ns"] or metadata.st_size != state["size"]):
            raise RecoveryError("recovery_conflict", "文件在读取元数据期间变化，请重试")
        sample = stream.read(ENCODING_PROBE_BYTES)
    complete = len(sample) == state["size"]
    encoding = None
    if b"\x00" in sample[:4096] and not sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "binary"
    else:
        candidates = ("utf-16",) if sample.startswith((b"\xff\xfe", b"\xfe\xff")) else (("utf-8-sig",) if sample.startswith(b"\xef\xbb\xbf") else ("utf-8", "gb18030"))
        for candidate in candidates:
            try:
                codecs.getincrementaldecoder(candidate)(errors="strict").decode(sample, final=complete)
                encoding = candidate
                break
            except UnicodeError:
                continue
    return {"encoding": encoding, "encoding_source": "full_small_file" if complete else "sample",
            "encoding_probe_bytes": len(sample), "text_read_within_limit": state["size"] <= 2_000_000 and encoding not in {None, "binary"}}
