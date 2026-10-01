import hashlib
from pathlib import Path

import pytest

from app import sandbox


@pytest.mark.parametrize("payload,encoding", [("中文".encode("utf-8"), "utf-8"), ("中文".encode("utf-16"), "utf-16"), (b"\x00binary", "binary"), (b"", "utf-8")])
def test_metadata_never_reads_whole_file_or_hashes_twice(tmp_path, monkeypatch, payload, encoding):
    path = tmp_path / "中文 file.txt"
    path.write_bytes(payload)
    original = sandbox._file_state
    calls = []

    def state(target):
        calls.append(target)
        return original(target)

    monkeypatch.setattr(sandbox, "_file_state", state)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("metadata decoded an entire file"))
    result = sandbox.execute_tool(str(tmp_path), "readonly", "file_metadata", {"path": path.name})
    assert result["success"] is True
    assert result["encoding"] == encoding
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert len(calls) == 1


def test_large_text_metadata_is_sampled_and_not_reported_as_editable(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * 3_000_000)
    result = sandbox.execute_tool(str(tmp_path), "readonly", "file_metadata", {"path": path.name})
    assert result["success"] is True
    assert result["encoding_source"] == "sample"
    assert result["encoding_probe_bytes"] <= 65536
    assert result["text_read_within_limit"] is False


def test_directory_metadata_does_not_probe_text(tmp_path):
    (tmp_path / "folder").mkdir()
    result = sandbox.execute_tool(str(tmp_path), "readonly", "file_metadata", {"path": "folder"})
    assert result["success"] is True
    assert result["encoding"] is None
    assert result["encoding_probe_bytes"] == 0
