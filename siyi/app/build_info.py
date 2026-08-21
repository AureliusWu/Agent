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
    packaged_root = getattr(sys, "_MEIPASS", None)
    if packaged_root:
        # A frozen sidecar must report the manifest shipped with that exact
        # payload.  The build process exports SIYI_BUILD_MANIFEST while it
        # runs, and child sidecars inherit it; looking at that environment
        # value first made an older baseline executable incorrectly report the
        # newly built candidate's identity during a comparison smoke test.
        candidates.append(Path(packaged_root) / "build-info.json")
    configured = os.environ.get("SIYI_BUILD_MANIFEST")
    if configured:
        candidates.append(Path(configured))
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
        "release_status": {
            "target_version": __version__,
            "source_version": __version__,
            "implementation_status": "UNKNOWN",
            "test_status": "NOT_READY",
            "distribution_status": "NOT_DISTRIBUTED",
        },
        "evidence_manifest_hash": "unavailable",
        "embedded": False,
    }


def _matches_runtime_schema(payload: dict[str, Any]) -> bool:
    """Reject a manifest whose database contract does not match this sidecar.

    ``build/generated/build-info.json`` is an untracked build output.  When a
    source checkout advances a database migration before the next package is
    built, that old output must not cause diagnostics to report a different
    schema from the running code.  Older manifests without this field remain
    readable for backwards compatibility; generated manifests always include
    it.
    """

    declared = payload.get("database_schema_version")
    if declared is None:
        return True
    try:
        return int(declared) == SCHEMA_VERSION
    except (TypeError, ValueError):
        return False


@lru_cache(maxsize=1)
def build_manifest() -> dict[str, Any]:
    for path in _candidate_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            payload.get("build_id")
            and payload.get("product_version")
            and _matches_runtime_schema(payload)
        ):
            return {**_fallback(), **payload, "embedded": True}
    return _fallback()


def sidecar_build_info() -> dict[str, Any]:
    manifest = build_manifest()
    return {
        **manifest,
        "component": "sidecar",
        "component_build_id": manifest.get("component_build_ids", {}).get("sidecar", "unavailable"),
    }
