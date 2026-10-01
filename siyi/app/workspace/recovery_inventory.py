"""Bounded, metadata-only recovery-area inventory; never deletes retained data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any

from app.workspace.file_recovery import checked_path, FORMAT_VERSION


MAX_INVENTORY_ENTRIES = 10_000
MAX_INVENTORY_SECONDS = 2.0


def list_recovery(workspace: str, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
    root = Path(workspace).resolve(strict=True)
    backup_root = checked_path(root, ".agent-backups")
    started = time.monotonic()
    folders: list[Path] = []
    truncated = False
    examined = 0
    if backup_root.is_dir():
        with os.scandir(backup_root) as entries:
            for item in entries:
                examined += 1
                if examined > MAX_INVENTORY_ENTRIES or time.monotonic() - started > MAX_INVENTORY_SECONDS:
                    truncated = True
                    break
                if re.fullmatch(r"\d+-[a-f0-9]{8}", item.name):
                    folders.append(Path(item.path))
    folders.sort(key=lambda item: int(item.name.split("-", 1)[0]), reverse=True)
    items = []
    for folder in folders[offset:offset + limit]:
        result: dict[str, Any] = {"change_id": folder.name, "status": "legacy", "paths": [],
                                  "eligibility": "not_checked", "retained_bytes": None, "capacity_status": "unknown"}
        try:
            checked_path(root, folder.relative_to(root).as_posix())
            record = checked_path(root, (folder / "manifest.json").relative_to(root).as_posix())
            if record.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("oversized manifest")
            manifest = json.loads(record.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("invalid manifest")
            entries = manifest.get("entries") or []
            if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
                raise ValueError("invalid manifest entries")
            result.update({key: manifest.get(key) for key in ("operation", "task_id", "created_at")})
            result["paths"] = [entry["path"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("path"), str)]
            recovery_record = manifest.get("recovery") or {}
            if not isinstance(recovery_record, dict):
                raise ValueError("invalid recovery status")
            recovery = recovery_record.get("status")
            if recovery == "restored":
                result["status"] = "restored"
            elif recovery in {"restoring", "needs_attention"}:
                result["status"] = "needs_attention"
            elif manifest.get("format_version") == FORMAT_VERSION and entries and all(entry.get("after") for entry in entries):
                result["status"] = "available"
            total_bytes = 0
            pending = [folder]
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as children:
                    for child in children:
                        examined += 1
                        if examined > MAX_INVENTORY_ENTRIES or time.monotonic() - started > MAX_INVENTORY_SECONDS:
                            raise TimeoutError("bounded recovery inventory")
                        metadata = child.stat(follow_symlinks=False)
                        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                            raise ValueError("link-like recovery entry")
                        if stat.S_ISDIR(metadata.st_mode):
                            pending.append(Path(child.path))
                        elif stat.S_ISREG(metadata.st_mode):
                            total_bytes += metadata.st_size
            result.update(retained_bytes=total_bytes, capacity_status="measured")
        except (OSError, ValueError, TypeError, KeyError):
            pass
        items.append(result)
    return {"items": items, "total": None if truncated else len(folders), "limit": limit,
            "offset": offset, "truncated": truncated, "capacity_scope": "returned_page"}
