from __future__ import annotations

import hashlib
import json
from pathlib import Path
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
