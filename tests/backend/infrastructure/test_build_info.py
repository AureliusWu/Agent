from __future__ import annotations

import json
import sys
from pathlib import Path

from app import build_info
from app.database import SCHEMA_VERSION


def _manifest(
    path: Path,
    *,
    build_id: str,
    fingerprint: str,
    database_schema_version: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "product_version": "13.0.0",
        "build_id": build_id,
        "source_fingerprint": fingerprint,
        "component_build_ids": {"sidecar": f"sidecar-{build_id}"},
    }
    if database_schema_version is not None:
        payload["database_schema_version"] = database_schema_version
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_frozen_sidecar_prefers_its_embedded_manifest_over_inherited_build_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A candidate build environment must not relabel an old frozen sidecar."""

    packaged_root = tmp_path / "baseline-sidecar"
    inherited_manifest = tmp_path / "candidate" / "build-info.json"
    _manifest(
        packaged_root / "build-info.json",
        build_id="baseline-build",
        fingerprint="baseline-fingerprint",
    )
    _manifest(
        inherited_manifest,
        build_id="candidate-build",
        fingerprint="candidate-fingerprint",
    )

    monkeypatch.setattr(sys, "_MEIPASS", str(packaged_root), raising=False)
    monkeypatch.setenv("SIYI_BUILD_MANIFEST", str(inherited_manifest))
    build_info.build_manifest.cache_clear()
    try:
        manifest = build_info.build_manifest()
    finally:
        build_info.build_manifest.cache_clear()

    assert manifest["embedded"] is True
    assert manifest["build_id"] == "baseline-build"
    assert manifest["source_fingerprint"] == "baseline-fingerprint"


def test_source_runtime_can_still_use_explicit_build_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "development" / "build-info.json"
    _manifest(
        manifest_path,
        build_id="development-build",
        fingerprint="development-fingerprint",
    )

    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setenv("SIYI_BUILD_MANIFEST", str(manifest_path))
    build_info.build_manifest.cache_clear()
    try:
        manifest = build_info.build_manifest()
    finally:
        build_info.build_manifest.cache_clear()

    assert manifest["embedded"] is True
    assert manifest["build_id"] == "development-build"


def test_runtime_ignores_manifest_with_mismatched_database_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stale_manifest = tmp_path / "stale" / "build-info.json"
    _manifest(
        stale_manifest,
        build_id="stale-build",
        fingerprint="stale-fingerprint",
        database_schema_version=SCHEMA_VERSION - 1,
    )

    monkeypatch.setattr(build_info, "_candidate_paths", lambda: [stale_manifest])
    build_info.build_manifest.cache_clear()
    try:
        manifest = build_info.build_manifest()
    finally:
        build_info.build_manifest.cache_clear()

    assert manifest["embedded"] is False
    assert manifest["database_schema_version"] == SCHEMA_VERSION
