"""Synthetic transport fixtures only: no actual RC/model/install/publication."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import urllib.request
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("rc_export_under_test", ROOT / "scripts/export-accepted-rc.py")
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
COMMIT = "a" * 40


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def transport_fixture(root):
    """Never a genuine accepted bundle; only exercise byte transport mechanics."""
    files = {}
    for name in exporter.REQUIRED_FILES:
        content = b'{"synthetic_fixture":true}' if name.endswith(".json") else b"synthetic transport only"
        files[name] = put(root, name, content)
    for kind, suffix in (("nsis", "-setup.exe"), ("msi", ".msi")):
        current = put(root, f"desktop/src-tauri/target/release/bundle/{kind}/synthetic16{suffix}", b"synthetic current " + kind.encode())
        previous = put(root, f"build/upgrade-baseline/synthetic8{suffix}", b"synthetic previous " + kind.encode())
        evidence = {"synthetic_fixture": True, "artifacts": {"candidate": {"name": Path(current["path"]).name, "bytes": current["bytes"], "sha256": current["sha256"]},
                                                             "previous": {"name": Path(previous["path"]).name, "bytes": previous["bytes"], "sha256": previous["sha256"]}}}
        name = f"build/v1600-evidence/{kind}-installer-smoke.json"
        files[name] = put(root, name, json.dumps(evidence).encode())
    return files


def transport_zip(root, output, *, entries=None, extras=(), index_mutation=None):
    entries = entries if entries is not None else exporter.index_from_closure(root, COMMIT)["files"]
    index = {"schema_version": 1, "target_version": "16.0.0", "source_commit": COMMIT,
             "accepted_candidate": True, "files": entries}
    if index_mutation:
        index_mutation(index)
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("artifact-index.json", json.dumps(index))
        for entry in entries:
            archive.write(root / entry["path"], entry["path"])
        for info, content in extras:
            archive.writestr(info, content)
    return output


def test_export_blocked_rc_creates_no_transport(tmp_path, monkeypatch):
    calls = []

    def refused(root):
        calls.append(root)
        raise exporter.TransportError("existing full RC is BLOCKED")

    monkeypatch.setattr(exporter, "verify_actual_rc", refused)
    output = tmp_path / "not-created.zip"
    with pytest.raises(exporter.TransportError):
        exporter.export_archive(tmp_path, output)
    assert calls == [tmp_path]
    assert not output.exists()


def test_closure_retains_hashed_raw_attachments_and_packages_without_writing(tmp_path):
    transport_fixture(tmp_path)
    evidence = put(tmp_path, "build/v1600-evidence/accepted/synthetic-junit.xml", b'<testsuite name="synthetic"/>')
    put(tmp_path, "build/v1600-evidence/accepted/rc-bundle.json", json.dumps({"synthetic_fixture": True, "raw": {"path": evidence["path"], "sha256": evidence["sha256"]}}).encode())
    index = exporter.index_from_closure(tmp_path, COMMIT)
    assert index["source_commit"] == COMMIT
    assert evidence in index["files"]
    assert len(index["files"]) == len(exporter.REQUIRED_FILES) + 5
    assert not (tmp_path / "artifact-index.json").exists()


def test_original_uppercase_reference_hashes_are_not_rewritten(tmp_path):
    transport_fixture(tmp_path)
    raw = put(tmp_path, "build/v1600-evidence/accepted/synthetic.xml", b"synthetic fixture")
    payload = json.dumps({"synthetic_fixture": True, "raw": {"path": raw["path"], "sha256": raw["sha256"].upper()}}).encode()
    path = tmp_path / "build/v1600-evidence/accepted/rc-bundle.json"
    path.write_bytes(payload)
    index = exporter.index_from_closure(tmp_path, COMMIT)
    assert raw in index["files"]
    assert path.read_bytes() == payload


@pytest.mark.parametrize("name", [
    "README.md", "../outside", "C:/repo/outside", "build\\private", "build/v1600-evidence/accepted/.env",
    "build/v1600-evidence/accepted/runtime.db", "build/v1600-evidence/accepted/conversation-export.json",
    "build/v1600-evidence/accepted/microphone.wav", "build/v1600-evidence/accepted/private/history.json",
    "build/v1600-evidence/accepted/CON.txt", "build/v1600-evidence/accepted/file:stream",
    "build/v1600-evidence/accepted/file. ", "build/v1600-evidence/accepted/private.zip",
])
def test_private_or_unsafe_paths_are_rejected(name):
    with pytest.raises(ValueError):
        exporter.public_path(name)


@pytest.mark.parametrize("content", [
    b"SQLite format 3\x00data", b'{"argv":"C:/repo/raw/report.json"}',
    b'{"argv":"/home/private-machine/raw"}', b'{"api_key":"private-value"}',
    b'{"endpoint":"https://account:password@example.invalid"}',
    b'{"email":"real.person@example.invalid"}', b"-----BEGIN " + b"PRIVATE" + b" KEY-----secret",
])
def test_private_raw_evidence_fails_without_redacting_bytes(tmp_path, content):
    transport_fixture(tmp_path)
    path = tmp_path / "build/v1600-evidence/accepted/default-model-identity.json"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        exporter.index_from_closure(tmp_path, COMMIT)
    assert path.read_bytes() == content


def test_sqlite_library_marker_is_not_misidentified_as_a_user_database():
    exporter.public_content("desktop/src-tauri/target/release/_internal/sqlite3.dll", b"MZ synthetic library constant SQLite format 3\x00")


@pytest.mark.parametrize("mutation", ["missing", "hash", "case_alias", "duplicate_key"])
def test_invalid_reference_closure_fails(tmp_path, mutation):
    transport_fixture(tmp_path)
    one = put(tmp_path, "build/v1600-evidence/accepted/raw.xml", b"synthetic")
    if mutation == "missing":
        (tmp_path / one["path"]).unlink()
    elif mutation == "hash":
        one["sha256"] = "0" * 64
    value = {"synthetic_fixture": True, "raw": one}
    if mutation == "case_alias":
        two = dict(one, path=one["path"].replace("raw.xml", "RAW.xml"))
        value["duplicate"] = two
    payload = json.dumps(value).encode()
    if mutation == "duplicate_key":
        payload = b'{"synthetic_fixture":true,"synthetic_fixture":false}'
    put(tmp_path, "build/v1600-evidence/accepted/rc-bundle.json", payload)
    with pytest.raises((ValueError, OSError)):
        exporter.index_from_closure(tmp_path, COMMIT)


def test_zip_receive_is_fresh_bounded_and_does_not_materialize_candidate(tmp_path):
    transport_fixture(tmp_path)
    archive = transport_zip(tmp_path, tmp_path / "synthetic.zip")
    received = tmp_path / "build/v1600-evidence/fresh-input"
    result = exporter.receive_archive(tmp_path, archive, received, COMMIT)
    assert result["status"] == "TRANSPORT_VALIDATED_NOT_RC_ACCEPTED"
    assert (received / "artifact-index.json").is_file()
    with pytest.raises(ValueError):
        exporter.receive_archive(tmp_path, archive, received, COMMIT)


@pytest.mark.parametrize("mutation", ["extra", "traversal", "case_alias", "symlink", "source", "hash", "private_index", "schema_type", "missing_required"])
def test_unsafe_zip_does_not_create_extraction_directory(tmp_path, mutation):
    transport_fixture(tmp_path)
    extras, mutate = [], None
    entries = exporter.index_from_closure(tmp_path, COMMIT)["files"]
    if mutation == "extra":
        extras = [("build/v1600-evidence/accepted/unindexed.json", b"{}")]
    elif mutation == "traversal":
        extras = [("../escaped.json", b"{}")]
    elif mutation == "case_alias":
        extras = [(entries[0]["path"].upper(), b"alias")]
    elif mutation == "symlink":
        info = zipfile.ZipInfo("build/v1600-evidence/accepted/linked.json")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        extras = [(info, b"../outside")]
    elif mutation == "source":
        mutate = lambda index: index.update(source_commit="b" * 40)
    elif mutation == "hash":
        entries[0]["sha256"] = "0" * 64
    elif mutation == "private_index":
        mutate = lambda index: index.update(private_machine="secret")
    elif mutation == "schema_type":
        mutate = lambda index: index.update(schema_version=True)
    else:
        entries = [entry for entry in entries if entry["path"] != "build/generated/build-info.json"]
    archive = transport_zip(tmp_path, tmp_path / "unsafe.zip", entries=entries, extras=extras, index_mutation=mutate)
    target = tmp_path / "build/v1600-evidence/never-created"
    with pytest.raises((ValueError, OSError)):
        exporter.receive_archive(tmp_path, archive, target, COMMIT)
    assert not target.exists()


def test_receive_dotdot_destination_cannot_escape_generated_root(tmp_path):
    transport_fixture(tmp_path)
    archive = transport_zip(tmp_path, tmp_path / "synthetic.zip")
    with pytest.raises(ValueError):
        exporter.receive_archive(tmp_path, archive, tmp_path / "build/v1600-evidence/../../outside", COMMIT)


@pytest.mark.parametrize("mutation", ["unindexed", "changed", "index_metadata", "schema_type", "required_removed"])
def test_expanded_transport_is_rechecked_before_upload(tmp_path, mutation):
    transport_fixture(tmp_path)
    archive = transport_zip(tmp_path, tmp_path / "synthetic.zip")
    directory = tmp_path / "build/v1600-evidence/fresh-input"
    exporter.receive_archive(tmp_path, archive, directory, COMMIT)
    if mutation == "unindexed":
        put(directory, "build/v1600-evidence/accepted/private-audio.wav", b"synthetic")
    elif mutation == "changed":
        put(directory, "build/v1600-evidence/accepted/default-model-identity.json", b"changed")
    else:
        path = directory / "artifact-index.json"
        index = json.loads(path.read_text())
        if mutation == "schema_type":
            index["schema_version"] = True
        elif mutation == "required_removed":
            name = "build/generated/build-info.json"
            index["files"] = [entry for entry in index["files"] if entry["path"] != name]
            (directory / name).unlink()
        else:
            index["private_metadata"] = "not allowed"
        path.write_text(json.dumps(index))
    with pytest.raises(ValueError):
        exporter.validate_directory(tmp_path, directory, COMMIT)


def test_hardlinked_attachment_is_not_exportable(tmp_path):
    transport_fixture(tmp_path)
    path = tmp_path / "build/v1600-evidence/accepted/default-model-identity.json"
    os.link(path, tmp_path / "synthetic-hardlink.json")
    with pytest.raises(ValueError):
        exporter.index_from_closure(tmp_path, COMMIT)


@pytest.mark.parametrize("limit", ["files", "bytes", "index"])
def test_transport_bounds_fail_before_extraction(tmp_path, monkeypatch, limit):
    transport_fixture(tmp_path)
    archive = transport_zip(tmp_path, tmp_path / "synthetic.zip")
    monkeypatch.setattr(exporter, {"files": "MAX_FILES", "bytes": "MAX_BYTES", "index": "MAX_INDEX_BYTES"}[limit], 1)
    with pytest.raises(ValueError):
        exporter.receive_archive(tmp_path, archive, tmp_path / "build/v1600-evidence/over-limit", COMMIT)
    assert not (tmp_path / "build/v1600-evidence/over-limit").exists()


def test_nested_frozen_library_cannot_hide_database(tmp_path):
    transport_fixture(tmp_path)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as library:
        library.writestr("synthetic.pyc", b"SQLite format 3\x00not-code")
    attachment = put(tmp_path, "desktop/src-tauri/target/release/_internal/base_library.zip", data.getvalue())
    index = exporter.index_from_closure(tmp_path, COMMIT)
    index["files"].append(attachment)
    archive = transport_zip(tmp_path, tmp_path / "synthetic-library.zip", entries=index["files"])
    with pytest.raises(ValueError):
        exporter.validate_archive(archive, COMMIT)


def test_actual_rc_verification_invokes_original_ten_gate_consumer_and_fails_closed(tmp_path, monkeypatch):
    transport_fixture(tmp_path)
    calls = []

    class Builder:
        def _release_source_identity(self, root):
            calls.append("source")
            return {"source_commit": COMMIT, "source_version": "16.0.0", "workspace_clean": True}

        def generate_manifest(self, root, build_type):
            calls.append("manifest")
            assert build_type == "Release"
            return {"synthetic_fixture": True}

    class Gate:
        def check_bundle(self, root, bundle, **kwargs):
            calls.append("original-full-rc")
            assert bundle == {"synthetic_fixture": True}
            return {"status": "BLOCKED", "passed": False}

    real_module = exporter.module
    monkeypatch.setattr(exporter, "module", lambda name: Builder() if name == "generate_build_info" else Gate() if name == "rc_gate" else real_module(name))
    with pytest.raises(ValueError):
        exporter.verify_actual_rc(tmp_path)
    assert calls == ["source", "manifest", "original-full-rc"]


def test_redirect_never_forwards_github_authorization():
    handler = exporter.AssetRedirects()
    request = urllib.request.Request("https://api.github.com/repos/Owner/Repo/releases/assets/1", headers={"Authorization": "Bearer synthetic", "Cookie": "synthetic=1"})
    redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/synthetic")
    assert redirected is not None
    assert not redirected.has_header("Authorization")
    assert not redirected.has_header("Cookie")
    for url in ["http://release-assets.githubusercontent.com/file", "https://attacker.invalid/file", "https://release-assets.githubusercontent.com:444/file", "https://account@release-assets.githubusercontent.com/file"]:
        with pytest.raises(ValueError):
            handler.redirect_request(request, None, 302, "Found", {}, url)


def test_own_repository_asset_download_is_digest_bound_and_mocked(tmp_path, monkeypatch):
    data = b"synthetic transport bytes, not a real RC"
    digest = hashlib.sha256(data).hexdigest()
    api = "https://api.github.com/repos/Owner/Repo/releases/assets/1"
    metadata = {"id": 1, "url": api, "state": "uploaded", "size": len(data), "name": "synthetic.zip",
                "browser_download_url": "https://github.com/Owner/Repo/releases/download/synthetic/synthetic.zip", "digest": "sha256:" + digest}
    calls = []

    class Response(io.BytesIO):
        def geturl(self):
            return api

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.get_header("Accept")))
            return Response(json.dumps(metadata).encode() if len(calls) == 1 else data)

    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    handlers = []
    def opener(*values):
        handlers.extend(values)
        return Opener()
    monkeypatch.setattr(exporter.urllib.request, "build_opener", opener)
    output = tmp_path / "download.zip"
    result = exporter.download_asset("Owner/Repo", 1, digest, output)
    assert result["status"] == "DOWNLOADED_NOT_ACCEPTED"
    assert output.read_bytes() == data
    assert calls == [(api, "application/vnd.github+json"), (api, "application/octet-stream")]
    assert any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies == {} for handler in handlers)
    metadata["browser_download_url"] = "https://github.com/Other/Repo/releases/download/synthetic/synthetic.zip"
    calls.clear()
    with pytest.raises(ValueError):
        exporter.download_asset("Owner/Repo", 1, digest, tmp_path / "blocked.zip")
    assert not (tmp_path / "blocked.zip").exists()


def test_workflow_preserves_original_consumers_and_uploads_only_after_success():
    workflow = yaml.safe_load((ROOT / ".github/workflows/rc-acceptance.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["accept"]
    assert "refs/heads/main" in job["if"]
    assert job["runs-on"] == "windows-latest"
    steps = job["steps"]
    upload = next(item for item in steps if item.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "${{ success() }}"
    assert upload["with"]["name"] == "Siyi-RC-Acceptance-v16.0.0"
    assert upload["with"]["path"] == "build/v1600-evidence/rc-input/"
    runs = "\n".join(item.get("run", "") for item in steps)
    for command in ["scripts/import-accepted-rc.py", "scripts/check-release-evidence.py", "scripts/rc_gate.py", "scripts/privacy_scan.py --tracked --history"]:
        assert command in runs
    assert runs.index("scripts/import-accepted-rc.py") < runs.index("scripts/rc_gate.py")
    assert "--sha256 $env:INPUT_ASSET_SHA256" in runs
    assert "--commit $env:GITHUB_SHA" in runs
    assert "pip install -r siyi/requirements.lock" in runs
    assert "build-desktop" not in runs and "gh release create" not in runs
