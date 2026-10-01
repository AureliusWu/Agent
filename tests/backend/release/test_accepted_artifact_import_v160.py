import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("accepted_import_tested", ROOT / "scripts/import-accepted-rc.py")
assert spec and spec.loader
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


@pytest.mark.parametrize("name", ["README.md", "siyi/app/config.py", "../build/fake", "C:/repo/outside.exe", "build/v1600-evidence/accepted/file:stream", "build\\unsafe"])
def test_import_cannot_modify_source_or_escape_generated_paths(name):
    with pytest.raises(ValueError):
        importer.allowed_path(name)


def fixture(root):
    directory = root / "build/v1600-evidence/rc-input"
    files = []
    for name in ["build/v1600-evidence/accepted/rc-bundle.json", "build/v1600-evidence/accepted/default-model-identity.json",
                 "build/v1600-evidence/nsis-installer-smoke.json", "build/v1600-evidence/msi-installer-smoke.json"]:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b'{"synthetic_fixture":true}'
        path.write_bytes(payload)
        files.append({"path": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    index = {"schema_version": 1, "target_version": "16.0.0", "source_commit": "a" * 40,
             "accepted_candidate": True, "files": files}
    (directory / "artifact-index.json").write_text(json.dumps(index), encoding="utf-8")
    return directory, index


def test_exact_generated_artifact_layout_is_read_only_validated(tmp_path):
    directory, _ = fixture(tmp_path)
    entries = importer.plan(directory, tmp_path, "a" * 40)
    assert len(entries) == 4
    assert not (tmp_path / "build/v1600-evidence/accepted").exists()


@pytest.mark.parametrize("mutation", ["source", "hash", "bytes", "duplicate", "missing", "existing"])
def test_unbound_changed_or_colliding_artifact_is_rejected(tmp_path, mutation):
    directory, index = fixture(tmp_path)
    if mutation == "source":
        index["source_commit"] = "b" * 40
    elif mutation == "hash":
        index["files"][0]["sha256"] = "0" * 64
    elif mutation == "bytes":
        index["files"][0]["bytes"] += 1
    elif mutation == "duplicate":
        index["files"].append(dict(index["files"][0]))
    elif mutation == "missing":
        index["files"].pop()
    else:
        target = tmp_path / index["files"][0]["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"existing different result")
    (directory / "artifact-index.json").write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ValueError):
        importer.plan(directory, tmp_path, "a" * 40)
