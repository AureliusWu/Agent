from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


class EvidenceError(ValueError):
    pass


def _version_tuple(value: object) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    if not match:
        raise EvidenceError(f"invalid release version: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"unable to read evidence {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"evidence {path.name} must contain one JSON object")
    return payload


def _require_true(mapping: dict[str, Any], fields: set[str], *, label: str) -> None:
    missing = sorted(field for field in fields if mapping.get(field) is not True)
    if missing:
        raise EvidenceError(f"{label} is missing true results: {', '.join(missing)}")


def _validate_artifacts(
    payload: dict[str, Any],
    *,
    root: Path,
    version: str,
    head: str,
    kind: str,
) -> None:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise EvidenceError(f"{kind} evidence has no artifacts object")
    candidate = artifacts.get("candidate")
    previous = artifacts.get("previous")
    build_manifest = artifacts.get("build_manifest")
    previous_manifest = artifacts.get("previous_build_manifest")
    for name, value in (
        ("candidate", candidate),
        ("previous", previous),
        ("build_manifest", build_manifest),
        ("previous_build_manifest", previous_manifest),
    ):
        if not isinstance(value, dict):
            raise EvidenceError(f"{kind} evidence artifact {name} is missing")
    assert isinstance(candidate, dict)
    assert isinstance(previous, dict)
    assert isinstance(build_manifest, dict)
    assert isinstance(previous_manifest, dict)
    if candidate.get("version") != version:
        raise EvidenceError(f"{kind} candidate is not version {version}")
    if _version_tuple(previous.get("version")) >= _version_tuple(version):
        raise EvidenceError(f"{kind} previous installer is not older than {version}")
    for label, artifact in (("candidate", candidate), ("previous", previous)):
        digest = str(artifact.get("sha256") or "")
        if not SHA256_RE.fullmatch(digest):
            raise EvidenceError(f"{kind} {label} installer has no SHA-256 binding")
    if str(candidate["sha256"]).upper() == str(previous["sha256"]).upper():
        raise EvidenceError(f"{kind} candidate and previous installer are identical")
    if (
        build_manifest.get("product_version") != version
        or build_manifest.get("git_commit") != head
        or build_manifest.get("workspace_state") != "CLEAN"
        or build_manifest.get("source_fingerprint")
        != payload["source"].get("source_tree_fingerprint")
        or build_manifest.get("build_id") != payload["source"].get("build_id")
    ):
        raise EvidenceError(f"{kind} candidate build manifest is not bound to clean HEAD")
    if previous_manifest.get("product_version") != previous.get("version"):
        raise EvidenceError(f"{kind} previous build manifest version is inconsistent")

    suffix = "-setup.exe" if kind == "NSIS" else ".msi"
    bundle = root / "desktop" / "src-tauri" / "target" / "release" / "bundle" / kind.lower()
    matches = [
        path
        for path in bundle.glob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name == candidate.get("name")
        and path.name.lower().endswith(suffix)
    ]
    if len(matches) != 1:
        raise EvidenceError(f"{kind} candidate referenced by evidence is missing from the bundle")
    if candidate.get("bytes") != matches[0].stat().st_size or matches[0].stat().st_size < 1024 * 1024:
        raise EvidenceError(f"{kind} candidate size is missing, inconsistent, or unexpectedly small")
    if _sha256(matches[0]) != str(candidate["sha256"]).upper():
        raise EvidenceError(f"{kind} bundle no longer matches its evidence hash")


def _validate_one(
    payload: dict[str, Any],
    *,
    root: Path,
    version: str,
    head: str,
    kind: str,
) -> None:
    expected_type = f"release_{kind.lower()}_installer_live_evidence"
    if payload.get("report_type") != expected_type:
        raise EvidenceError(f"{kind} report_type must be {expected_type}")
    if payload.get("status") != "PASS" or payload.get("actual_run") is not True:
        raise EvidenceError(f"{kind} evidence is not an actual passing run")
    if payload.get("target_version") != version:
        raise EvidenceError(f"{kind} evidence target does not match VERSION={version}")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise EvidenceError(f"{kind} evidence has no source identity")
    if (
        source.get("source_version") != version
        or source.get("source_commit") != head
        or source.get("workspace_clean") is not True
        or not SHA256_RE.fullmatch(str(source.get("source_tree_fingerprint") or ""))
        or not re.fullmatch(r"[0-9a-f]{24}", str(source.get("build_id") or ""))
    ):
        raise EvidenceError(f"{kind} evidence is not bound to clean VERSION and HEAD")
    run = payload.get("run")
    if not isinstance(run, dict) or run.get("installer_kind") != kind or run.get("isolated_test_data") is not True:
        raise EvidenceError(f"{kind} run did not use isolated test-owned data")
    checks = payload.get("checks")
    if not isinstance(checks, dict) or not checks:
        raise EvidenceError(f"{kind} evidence has no checks")
    failed_checks = sorted(
        name
        for name, result in checks.items()
        if not isinstance(result, dict) or result.get("passed") is not True
    )
    if failed_checks:
        raise EvidenceError(f"{kind} evidence contains failed checks: {', '.join(failed_checks)}")
    results = payload.get("results")
    if not isinstance(results, dict) or results.get("status") != "ok" or results.get("version") != version:
        raise EvidenceError(f"{kind} evidence has no successful result object")
    common = {
        "desktop_started",
        "sidecar_stopped",
        "previous_version_upgrade",
        "schema_migrated",
        "in_place_upgrade_preserved_data",
        "uninstall_preserved_data",
        "reinstall_started" if kind == "NSIS" else "reinstall",
        "reinstall_recognized_data",
        "final_uninstall",
        "package_files_removed",
    }
    if kind == "MSI":
        common |= {"install", "uninstall", "uninstall_preserved_models"}
    _require_true(results, common, label=f"{kind} lifecycle")
    _validate_artifacts(payload, root=root, version=version, head=head, kind=kind)


def validate_release_evidence(root: Path, evidence_root: Path) -> None:
    version = (root / "VERSION").read_text(encoding="ascii").strip()
    _version_tuple(version)
    expected_root = root / "build" / f"v{re.sub(r'[^0-9A-Za-z]', '', version)}-evidence"
    if evidence_root.resolve() != expected_root.resolve():
        raise EvidenceError(f"evidence root must be {expected_root}")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _validate_one(
        _read_json(evidence_root / "nsis-installer-smoke.json"),
        root=root,
        version=version,
        head=head,
        kind="NSIS",
    )
    _validate_one(
        _read_json(evidence_root / "msi-installer-smoke.json"),
        root=root,
        version=version,
        head=head,
        kind="MSI",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate source-bound Windows release evidence")
    parser.add_argument("--evidence-root", type=Path)
    arguments = parser.parse_args()
    version = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    evidence_root = arguments.evidence_root or (
        ROOT / "build" / f"v{re.sub(r'[^0-9A-Za-z]', '', version)}-evidence"
    )
    try:
        validate_release_evidence(ROOT, evidence_root)
    except (EvidenceError, OSError, subprocess.SubprocessError) as exc:
        print(f"Release evidence rejected: {exc}", file=sys.stderr)
        return 1
    print(f"Release evidence is valid for v{version} and current HEAD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
