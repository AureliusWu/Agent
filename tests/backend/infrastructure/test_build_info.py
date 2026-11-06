from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app import __version__, build_info
from app.database import SCHEMA_VERSION


def _manifest(
    path: Path,
    *,
    build_id: str,
    fingerprint: str,
    database_schema_version: int | None = None,
    product_version: str = __version__,
    release_status: dict[str, str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "product_version": product_version,
        "build_id": build_id,
        "source_fingerprint": fingerprint,
        "component_build_ids": {"sidecar": f"sidecar-{build_id}"},
    }
    if database_schema_version is not None:
        payload["database_schema_version"] = database_schema_version
    if release_status is not None:
        payload["release_status"] = release_status
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


@pytest.mark.parametrize("version_fault", ["product_version", "source_version", "target_version"])
def test_runtime_ignores_stale_manifest_even_with_matching_schema(tmp_path, monkeypatch, version_fault):
    stale = tmp_path / "stale-build-info.json"
    status = {"source_version": __version__, "target_version": __version__}
    if version_fault in status:
        status[version_fault] = "0.0.1"
    _manifest(
        stale,
        build_id="stale",
        fingerprint="stale",
        database_schema_version=SCHEMA_VERSION,
        product_version="0.0.1" if version_fault == "product_version" else __version__,
        release_status=status,
    )
    monkeypatch.setattr(build_info, "_candidate_paths", lambda: [stale])
    build_info.build_manifest.cache_clear()
    try:
        manifest = build_info.build_manifest()
    finally:
        build_info.build_manifest.cache_clear()
    assert manifest["embedded"] is False
    assert manifest["product_version"] == manifest["release_status"]["source_version"] == __version__
    assert manifest["build_id"] == "unavailable"


@pytest.mark.parametrize("frozen", [True, False])
def test_invalid_authoritative_manifest_never_inherits_another_build_identity(tmp_path, monkeypatch, frozen):
    checkout_default = tmp_path / "build" / "generated" / "build-info.json"
    inherited = tmp_path / "inherited" / "build-info.json"
    bundled = tmp_path / "bundle" / "build-info.json"
    _manifest(checkout_default, build_id="checkout", fingerprint="checkout")
    monkeypatch.setattr(build_info, "__file__", str(tmp_path / "siyi" / "app" / "build_info.py"))
    monkeypatch.setenv("SIYI_BUILD_MANIFEST", str(inherited))
    if frozen:
        _manifest(bundled, build_id="stale-bundle", fingerprint="stale", product_version="0.0.1")
        _manifest(inherited, build_id="other-build", fingerprint="other-build")
        monkeypatch.setattr(sys, "_MEIPASS", str(bundled.parent), raising=False)
    else:
        _manifest(inherited, build_id="stale-explicit", fingerprint="stale", product_version="0.0.1")
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    build_info.build_manifest.cache_clear()
    try:
        manifest = build_info.build_manifest()
    finally:
        build_info.build_manifest.cache_clear()
    assert manifest["embedded"] is False
    assert manifest["build_id"] == "unavailable"
