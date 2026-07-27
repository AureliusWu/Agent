from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import __version__
from .database import SCHEMA_VERSION


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("SIYI_BUILD_MANIFEST")
    if configured:
        candidates.append(Path(configured))
    packaged_root = getattr(sys, "_MEIPASS", None)
    if packaged_root:
        candidates.append(Path(packaged_root) / "build-info.json")
    candidates.append(Path(__file__).resolve().parents[2] / "build" / "generated" / "build-info.json")
    return candidates


def _fallback() -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "product_version": __version__,
        "git_commit": "unknown",
        "git_short_commit": "unknown",
        "git_branch": "unknown",
        "build_time": "unknown",
        "build_type": "Development",
        "workspace_state": "UNKNOWN",
        "source_fingerprint": "unknown",
        "build_id": "unavailable",
        "component_build_ids": {"tauri": "unavailable", "react": "unavailable", "sidecar": "unavailable"},
        "database_schema_version": SCHEMA_VERSION,
        "embedded": False,
    }


@lru_cache(maxsize=1)
def build_manifest() -> dict[str, Any]:
    for path in _candidate_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("build_id") and payload.get("product_version"):
            return {**payload, "embedded": True}
    return _fallback()


def sidecar_build_info() -> dict[str, Any]:
    manifest = build_manifest()
    return {
        **manifest,
        "component": "sidecar",
        "component_build_id": manifest.get("component_build_ids", {}).get("sidecar", "unavailable"),
    }
