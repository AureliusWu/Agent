from __future__ import annotations

import ast
import base64
from contextlib import redirect_stdout
import hashlib
import io
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


@pytest.mark.parametrize("separator", ["/", "\\"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_exact_upstream_binary_exempts_only_reviewed_profile_needles(tmp_path: Path, monkeypatch,
                                                                   separator: str, encoding: str) -> None:
    profile = separator.join(("C:", "Users", "runneradmin"))
    cargo = separator.join((profile, ".cargo"))
    nested = separator.join((cargo, "registry", "src", "index.crates.io-fixture", "vendor.rs"))
    data = b"MZ" + nested.encode(encoding)
    file = tmp_path / "wheel-fixture.pyd"
    file.write_bytes(data)
    name = "_internal/locked_vendor/wheel-fixture.pyd"
    monkeypatch.setattr(PREFLIGHT, "OFFICIAL_VENDOR_MEMBERS", {name: (len(data), hashlib.sha256(data).hexdigest())})
    blocked = tuple(path.lower().encode(encoding) for path in (profile, cargo))

    PREFLIGHT.scan_file(file, name, blocked)
    assert file.read_bytes() == data
    with pytest.raises(CandidateError, match="build-machine path"):
        PREFLIGHT.scan_file(file, name.upper(), blocked)
    with pytest.raises(CandidateError, match="build-machine path"):
        PREFLIGHT.scan_file(file, name, blocked + (nested.lower().encode(encoding),))

    file.write_bytes(b"MZ" + b"?" * (len(data) - 2))
    with pytest.raises(CandidateError, match="official upstream resource identity differs"):
        PREFLIGHT.scan_file(file, name, blocked)
    file.write_bytes(data + b"?")
    with pytest.raises(CandidateError, match="official upstream resource identity differs"):
        PREFLIGHT.scan_file(file, name, blocked)

    other_profile_path = data + b"\0\0" + separator.join((profile, "private-fixture")).encode(encoding)
    file.write_bytes(other_profile_path)
    monkeypatch.setattr(PREFLIGHT, "OFFICIAL_VENDOR_MEMBERS", {
        name: (len(other_profile_path), hashlib.sha256(other_profile_path).hexdigest()),
    })
    with pytest.raises(CandidateError, match="build-machine path"):
        PREFLIGHT.scan_file(file, name, blocked)


def test_exact_upstream_binary_still_runs_other_path_and_credential_checks(tmp_path: Path, monkeypatch) -> None:
    profile = "/".join(("c:", "users", "runneradmin")).encode()
    unrelated = b"other-host-temp-fixture"
    secret_like = b"AK" + b"IA" + b"Z" * 16
    data = b"MZ" + profile + b"/.cargo/registry/src/index.crates.io-fixture" + b"\0" + unrelated + b"\0" + secret_like
    file = tmp_path / "wheel-fixture.pyd"
    file.write_bytes(data)
    name = "_internal/locked_vendor/wheel-fixture.pyd"
    monkeypatch.setattr(PREFLIGHT, "OFFICIAL_VENDOR_MEMBERS", {name: (len(data), hashlib.sha256(data).hexdigest())})

    with pytest.raises(CandidateError, match="build-machine path"):
        PREFLIGHT.scan_file(file, name, (profile, unrelated))
    with pytest.raises(ValueError, match="credential-bearing"):
        PREFLIGHT.scan_file(file, name, (profile,))


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


def test_summary_stdout_is_ascii_even_with_cp1252_pipe(tmp_path: Path) -> None:
    summary = {"status": "PREFLIGHT_PASS", "installer": {"path": "nsis/司忆_17.0.0_x64-setup.exe"}}
    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding="cp1252", errors="strict") as stdout, redirect_stdout(stdout):
        PREFLIGHT.write_public_summary(tmp_path, summary)
        stdout.flush()
        emitted = buffer.getvalue()
    assert emitted.isascii() and json.loads(emitted) == summary
    artifact = (tmp_path / "summary.json").read_bytes()
    assert "司忆".encode("utf-8") in artifact and json.loads(artifact) == summary


def test_msi_failure_reader_projects_only_valid_fixed_fields(tmp_path: Path) -> None:
    log = tmp_path / "installed-smoke.log"
    typed = {"stage": "upgrade", "reason": "MSI_EXIT", "msi_exit": 1603}
    prefix = PREFLIGHT.MSI_CHILD_PREFIX
    marker = prefix + json.dumps(typed).encode() + b"\r\n"
    log.write_bytes(b"ignore private log data\n" + marker + b"unstructured trailing data\n")
    child = PREFLIGHT.read_msi_child_failure(log)
    assert child == typed
    diagnostics = PREFLIGHT.Diagnostics()
    diagnostics.mark(PREFLIGHT.Stage.INSTALL, PREFLIGHT.Role.MSI)
    diagnostics.msi_child = child
    error = CandidateError("candidate subprocess failed; private log retained")
    assert diagnostics.public_failure("observe", error)["msi_child"] == typed
    assert "msi_child" not in diagnostics.public_failure("build", error)
    diagnostics.mark(PREFLIGHT.Stage.INSTALL, PREFLIGHT.Role.NSIS)
    diagnostics.msi_child = child
    assert "msi_child" not in diagnostics.public_failure("observe", error)
    diagnostics.mark(PREFLIGHT.Stage.VERIFY_INSTALL, PREFLIGHT.Role.MSI)
    assert diagnostics.msi_child is None


@pytest.mark.parametrize("raw", [
    b'{"stage":"private-file-fixture","reason":"MSI_EXIT"}',
    b'{"stage":"upgrade","reason":"unreviewed message"}',
    b'{"stage":"upgrade","reason":"MSI_EXIT","msi_exit":true}',
    b'{"stage":"upgrade","reason":"MSI_EXIT","msi_exit":65536}',
    b'{"stage":"upgrade","reason":"MSI_EXIT","msi_exit":0}',
    b'{"stage":"launch_candidate","reason":"NATIVE_EXITED","msi_exit":1}',
    b'{"stage":"upgrade","stage":"prepare","reason":"MSI_EXIT"}',
    b'{"stage":"upgrade","reason":"MSI_EXIT","raw_error":"private-fixture"}',
    b'{"stage":[],"reason":"MSI_EXIT"}',
])
def test_msi_failure_reader_rejects_unknown_duplicate_or_invalid_fields(tmp_path: Path, raw: bytes) -> None:
    log = tmp_path / "installed-smoke.log"
    log.write_bytes(PREFLIGHT.MSI_CHILD_PREFIX + raw + b"\n")
    assert PREFLIGHT.read_msi_child_failure(log) is None


def test_msi_failure_reader_rejects_unbounded_or_ambiguous_log(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "installed-smoke.log"
    marker = PREFLIGHT.MSI_CHILD_PREFIX + b'{"stage":"launch_candidate","reason":"NATIVE_WINDOW_MISSING"}\n'
    log.write_bytes(marker)
    assert PREFLIGHT.read_msi_child_failure(log) == {"stage": "launch_candidate", "reason": "NATIVE_WINDOW_MISSING"}
    log.write_bytes(marker * 2)
    assert PREFLIGHT.read_msi_child_failure(log) is None
    log.write_bytes(marker.rstrip(b"\n"))
    assert PREFLIGHT.read_msi_child_failure(log) is None
    log.write_bytes(marker)
    monkeypatch.setattr(PREFLIGHT, "MSI_CHILD_LOG_LIMIT", len(marker) - 1)
    assert PREFLIGHT.read_msi_child_failure(log) is None


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
