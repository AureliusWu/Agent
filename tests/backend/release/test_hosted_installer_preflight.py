from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from rc_desktop_candidate import CandidateError, module
finally:
    sys.path.pop(0)

PREFLIGHT = module("hosted-installer-preflight")
SOURCE = {"source_version": "17.0.0", "source_commit": "a" * 40, "workspace_clean": True,
          "source_tree_fingerprint": "B" * 64}


def make_archive(tmp_path: Path, mutation: str = "") -> tuple[Path, str, dict]:
    packages = {}
    data = {}
    for kind, name in PREFLIGHT.package_paths(SOURCE["source_version"]).items():
        contents = kind.encode().ljust(1024**2, b"\x00")
        packages[kind] = {"path": name, "bytes": len(contents), "sha256": hashlib.sha256(contents).hexdigest()}
        data[name] = contents
    descriptor = {"source": {**SOURCE, "build_id": "c" * 24}, "packages": packages}
    encoded = json.dumps(descriptor).encode()
    output = tmp_path / "fixture.zip"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("candidate.json", encoded)
        for name, contents in data.items():
            if mutation == "installer" and name.startswith("msi/"):
                contents = b"M" + contents[1:]
            archive.writestr(name, contents)
        if mutation == "traversal":
            archive.writestr("../unexpected.txt", b"no")
    descriptor_sha = hashlib.sha256(encoded).hexdigest() if mutation != "descriptor" else "d" * 64
    return output, descriptor_sha, descriptor


def test_hosted_preflight_receives_same_two_installers(tmp_path: Path) -> None:
    archive, descriptor_sha, descriptor = make_archive(tmp_path)
    destination = tmp_path / "bundle"
    assert PREFLIGHT.extract(archive, destination, SOURCE, descriptor_sha) == descriptor
    assert PREFLIGHT.package_files(destination, SOURCE["source_version"]) == descriptor["packages"]


@pytest.mark.parametrize(("mutation", "error"), [
    ("installer", "candidate bytes differ"),
    ("descriptor", "descriptor differs from producer"),
    ("traversal", "unexpected candidate ZIP member"),
])
def test_hosted_preflight_rejects_changed_transport(tmp_path: Path, mutation: str, error: str) -> None:
    archive, descriptor_sha, _ = make_archive(tmp_path, mutation)
    with pytest.raises(CandidateError, match=error):
        PREFLIGHT.extract(archive, tmp_path / "bundle", SOURCE, descriptor_sha)
    assert not (tmp_path / "unexpected.txt").exists()


def test_hosted_preflight_rejects_private_payload_before_upload(tmp_path: Path) -> None:
    file = tmp_path / "payload.bin"
    marker = b"some-test-profile-path"
    file.write_bytes(b"X" * (1024**2 - 3) + marker)
    with pytest.raises(CandidateError, match="build-machine path"):
        PREFLIGHT.scan_file(file, "payload.bin", (marker,))
    file.write_bytes(b"SQLite format 3\x00")
    with pytest.raises(ValueError, match="private database"):
        PREFLIGHT.scan_file(file, "payload.bin")


def test_only_exact_public_pillow_font_false_positive_is_accepted(tmp_path: Path) -> None:
    from PIL import ImageFont

    source = Path(ImageFont.__file__).read_text(encoding="utf-8")
    font, = (
        node.value for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes)
        and len(node.value) == 17_125
        and hashlib.sha256(node.value).hexdigest() == "c548006485e312aafaf58d372935ed7647674d1e61ea0402beff7d3eaf892f04"
    )
    assert base64.b64decode(b"".join(font.split()), validate=True).startswith(b"\x00\x01\x00\x00")
    match = module("privacy_scan").SECRET_PATTERNS["aws_access_key"].search(font)
    assert match is not None
    window_start = match.start() - PREFLIGHT.PUBLIC_PILLOW_FONT_AWS_MATCH_OFFSET
    window_end = window_start + PREFLIGHT.PUBLIC_PILLOW_FONT_CONTEXT_BYTES
    assert hashlib.sha256(font[window_start:window_end]).hexdigest() == PREFLIGHT.PUBLIC_PILLOW_FONT_CONTEXT_SHA256
    sidecar = tmp_path / "agent-backend.exe"
    original = b"MZ" + font
    sidecar.write_bytes(original)
    PREFLIGHT.scan_file(sidecar, "agent-backend.exe")
    assert sidecar.read_bytes() == original
    with pytest.raises(ValueError, match="credential-bearing"):
        PREFLIGHT.scan_file(sidecar, "other.exe")

    secret_like = b"AK" + b"IA" + b"Z" * 16
    tampered = b"MZ" + font[:window_start] + b"?" + font[window_start + 1:]
    for payload in (original + b"\0" + secret_like, tampered, original + font):
        sidecar.write_bytes(payload)
        with pytest.raises(ValueError, match="credential-bearing"):
            PREFLIGHT.scan_file(sidecar, "agent-backend.exe")


def test_private_bundle_inputs_are_ordinary_copies_and_do_not_patch_cargo_links(tmp_path: Path) -> None:
    cargo_desktop = tmp_path / "cargo-desktop.exe"
    cargo_desktop.write_bytes(b"MZ" + PREFLIGHT.TAURI_MARKER)
    alias = tmp_path / "cargo-deps-app.exe"
    os.link(cargo_desktop, alias)
    transport = module("export-accepted-rc")
    with pytest.raises(transport.TransportError, match="only unlinked ordinary files"):
        transport.ordinary(cargo_desktop)

    portable = tmp_path / "portable"
    portable.mkdir()
    shutil.copyfile(cargo_desktop, portable / "司忆.exe")
    (portable / "agent-backend.exe").write_bytes(b"MZsidecar-fixture")
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in portable.iterdir()}
    target = tmp_path / "bundle-target"
    staged = PREFLIGHT.stage_bundle_inputs(portable, target, hashes)
    assert {path.name: sha for path, sha in staged.items()} == hashes
    assert all(path.parent == target / "release" and transport.ordinary(path).st_nlink == 1 for path in staged)
    staged_desktop = target / "release" / "司忆.exe"
    assert not staged_desktop.samefile(cargo_desktop)
    assert not staged_desktop.samefile(portable / "司忆.exe")
    staged_desktop.write_bytes(b"MZ__TAURI_BUNDLE_TYPE_VAR_NSS")
    assert cargo_desktop.samefile(alias)
    assert cargo_desktop.read_bytes() == alias.read_bytes() == (portable / "司忆.exe").read_bytes()
    with pytest.raises(FileExistsError):
        PREFLIGHT.stage_bundle_inputs(portable, target, hashes)
    with pytest.raises(CandidateError, match="private bundle input differs"):
        PREFLIGHT.stage_bundle_inputs(portable, tmp_path / "other-target", {**hashes, "司忆.exe": "0" * 64})


def test_failure_report_uses_fixed_stage_reason_and_relative_digest(tmp_path: Path, monkeypatch, capsys) -> None:
    private_name = "_internal/never-print-my-filename.bin"
    file = tmp_path / "blocked.bin"
    file.write_bytes(b"SQLite format 3\x00")

    def blocked_build(source, private, diagnostics):
        diagnostics.mark(PREFLIGHT.Stage.SCAN_PAYLOAD, PREFLIGHT.Role.SUPPORT, private_name)
        PREFLIGHT.scan_file(file, private_name)

    monkeypatch.setattr(PREFLIGHT, "context", lambda: (SOURCE, tmp_path))
    monkeypatch.setattr(PREFLIGHT, "build", blocked_build)
    assert PREFLIGHT.main(["build"]) == 1
    result = capsys.readouterr()
    assert result.out == ""
    assert json.loads(result.err) == {
        "event": "installer_preflight_failed", "phase": "build", "stage": "scan_payload",
        "reason": "PRIVATE_DATABASE_OR_KEY", "role": "sidecar_support",
        "relative_path_sha256": hashlib.sha256(private_name.encode("utf-8")).hexdigest(),
    }
    assert private_name not in result.err and "SQLite" not in result.err


def test_unreviewed_error_and_invalid_argument_do_not_echo_raw_text(tmp_path: Path, monkeypatch, capsys) -> None:
    private = str(tmp_path / "never-print-my-argument")

    def fail():
        raise ValueError(private + "\n::error:: forged annotation")

    monkeypatch.setattr(PREFLIGHT, "context", fail)
    assert PREFLIGHT.main(["build"]) == 1
    result = capsys.readouterr()
    assert json.loads(result.err) == {"event": "installer_preflight_failed", "phase": "build",
                                     "stage": "context", "reason": "VALIDATION_FAILED"}
    assert private not in result.err and "forged annotation" not in result.err
    assert PREFLIGHT.main(["receive", "--artifact-id", private]) == 1
    result = capsys.readouterr()
    assert json.loads(result.err) == {"event": "installer_preflight_failed", "phase": "unknown",
                                     "stage": "context", "reason": "INVALID_ARGUMENTS"}
    assert private not in result.err


def test_hosted_preflight_is_explicit_and_never_grants_release_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(CandidateError, match="own-main exact-SHA hosted dispatch"):
        PREFLIGHT.context()
    workflow = yaml.load((ROOT / PREFLIGHT.WORKFLOW).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert list(workflow["on"]) == ["workflow_dispatch"]
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    jobs = workflow["jobs"]
    assert jobs["installed"]["needs"] == "bundle"
    assert jobs["installed"]["strategy"]["matrix"]["kind"] == ["nsis", "msi"]
    assert all(job["runs-on"] == "windows-2025" for job in jobs.values())
    assert [step["name"] for step in jobs["installed"]["steps"] if "GH_TOKEN" in step.get("env", {})] == [
        "Verify and receive exact candidate and official upgrade baseline",
    ]
