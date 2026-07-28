from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.sandbox import file_version_token
from app.tools.file_operations import (
    CORE_FILE_OPERATIONS,
    MAX_BATCH_OPERATIONS,
    FileOperationRequest,
    execute_file_batch,
    execute_file_operation,
)


def request(operation: str, **arguments) -> FileOperationRequest:
    return FileOperationRequest(operation, arguments)


def test_core_operation_catalog_contains_all_v920_operations() -> None:
    assert set(CORE_FILE_OPERATIONS) == {
        "file.read",
        "file.write",
        "file.patch",
        "file.copy",
        "file.move",
        "file.rename",
        "file.delete",
        "file.restore",
        "file.search",
        "file.list",
        "file.stat",
        "directory.create",
        "directory.list",
        "directory.move",
        "directory.delete",
    }


def test_create_read_patch_rename_move_delete_restore_round_trip(tmp_path: Path) -> None:
    created = execute_file_operation(
        str(tmp_path),
        request("file.write", path="notes.txt", content="alpha\n", expected_version_token="missing"),
    )
    assert created["success"] is True
    read = execute_file_operation(str(tmp_path), request("file.read", path="notes.txt"))
    assert read["content"] == "alpha"
    patched = execute_file_operation(
        str(tmp_path),
        request(
            "file.patch",
            path="notes.txt",
            patch="@@ -1 +1 @@\n-alpha\n+beta\n",
            expected_version_token=read["version_token"],
        ),
    )
    assert patched["success"] is True
    renamed = execute_file_operation(
        str(tmp_path),
        request(
            "file.rename",
            source="notes.txt",
            destination="renamed.txt",
            expected_version_token=file_version_token(tmp_path / "notes.txt"),
            expected_destination_version_token="missing",
        ),
    )
    assert renamed["success"] is True
    moved = execute_file_operation(
        str(tmp_path),
        request(
            "file.move",
            source="renamed.txt",
            destination="nested/final.txt",
            expected_version_token=file_version_token(tmp_path / "renamed.txt"),
            expected_destination_version_token="missing",
        ),
    )
    deleted = execute_file_operation(
        str(tmp_path),
        request(
            "file.delete",
            path="nested/final.txt",
            expected_version_token=file_version_token(tmp_path / "nested" / "final.txt"),
        ),
    )
    assert moved["success"] is True and deleted["success"] is True
    restored = execute_file_operation(
        str(tmp_path),
        request("file.restore", change_id=deleted["change_id"]),
    )
    assert restored["success"] is True
    assert (tmp_path / "nested" / "final.txt").read_text(encoding="utf-8") == "beta\n"


def test_every_mutation_dry_run_has_zero_filesystem_effect(tmp_path: Path) -> None:
    result = execute_file_operation(
        str(tmp_path),
        request("file.write", path="preview.txt", content="preview", expected_version_token="missing"),
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert "+preview" in result["diff"]
    assert not (tmp_path / "preview.txt").exists()
    assert not (tmp_path / ".agent-backups").exists()


def test_directory_delete_is_bounded_recoverable_and_dry_runnable(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "nested").mkdir(parents=True)
    (tree / "nested" / "item.txt").write_text("content", encoding="utf-8")
    version = file_version_token(tree)

    preview = execute_file_operation(
        str(tmp_path),
        request("directory.delete", path="tree", expected_version_token=version, max_entries=10),
        dry_run=True,
    )
    assert preview["entry_count"] == 2
    assert tree.exists()

    deleted = execute_file_operation(
        str(tmp_path),
        request("directory.delete", path="tree", expected_version_token=version, max_entries=10),
    )
    assert deleted["success"] is True and not tree.exists()
    restored = execute_file_operation(
        str(tmp_path),
        request("file.restore", change_id=deleted["change_id"]),
    )
    assert restored["success"] is True
    assert (tree / "nested" / "item.txt").read_text(encoding="utf-8") == "content"


def test_batch_failure_rolls_back_all_completed_changes(tmp_path: Path) -> None:
    result = execute_file_batch(
        str(tmp_path),
        [
            request("file.write", path="first.txt", content="one", expected_version_token="missing"),
            request("file.write", path="../outside.txt", content="escape", expected_version_token="missing"),
        ],
    )

    assert result["success"] is False
    assert result["failed_index"] == 1
    assert result["rolled_back"] is True
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path.parent / "outside.txt").exists()


def test_batch_limit_is_enforced_before_any_write(tmp_path: Path) -> None:
    requests = [
        request("file.write", path=f"{index}.txt", content="x", expected_version_token="missing")
        for index in range(MAX_BATCH_OPERATIONS + 1)
    ]

    result = execute_file_batch(str(tmp_path), requests)

    assert result["error_code"] == "batch_limit_exceeded"
    assert not list(tmp_path.glob("*.txt"))


def test_same_name_conflict_and_missing_path_are_structured(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    (tmp_path / "target.txt").write_text("target", encoding="utf-8")
    conflict = execute_file_operation(
        str(tmp_path),
        request(
            "file.move",
            source="source.txt",
            destination="target.txt",
            expected_version_token=file_version_token(tmp_path / "source.txt"),
            expected_destination_version_token="missing",
        ),
    )
    missing = execute_file_operation(str(tmp_path), request("file.read", path="missing.txt"))

    assert conflict["error_code"] == "version_conflict"
    assert missing["error_code"] == "io_error"
    assert missing["error_message"]


def test_non_utf8_and_oversized_files_are_not_silently_corrupted(tmp_path: Path) -> None:
    (tmp_path / "gb.txt").write_bytes("中文内容\r\n".encode("gb18030"))
    decoded = execute_file_operation(str(tmp_path), request("file.read", path="gb.txt", encoding="auto"))
    assert decoded["encoding"] == "gb18030"
    assert decoded["content"] == "中文内容"

    (tmp_path / "huge.bin").write_bytes(b"x" * 2_000_001)
    oversized = execute_file_operation(str(tmp_path), request("file.read", path="huge.bin"))
    assert oversized["success"] is False
    assert "2 MB" in oversized["error_message"]


def test_locked_or_denied_atomic_write_returns_complete_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "locked.txt"
    path.write_text("before", encoding="utf-8")

    def denied(*_args, **_kwargs):
        raise PermissionError(13, "denied", str(path))

    monkeypatch.setattr("app.sandbox.os.replace", denied)
    result = execute_file_operation(
        str(tmp_path),
        request(
            "file.write",
            path="locked.txt",
            content="after",
            expected_version_token=file_version_token(path),
        ),
    )

    assert result["error_code"] == "io_error"
    assert result["retryable"] is True
    assert result["io_error"]["type"] == "PermissionError"
    assert result["io_error"]["errno"] == 13
    assert path.read_text(encoding="utf-8") == "before"


def test_dotted_core_operation_is_available_through_the_tools_api(tmp_path: Path) -> None:
    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "full"},
        ).json()
        result = client.post(
            "/api/tools/execute",
            json={
                "conversation_id": conversation["id"],
                "workspace": str(tmp_path),
                "permission_mode": "full",
                "tool": "file.write",
                "arguments": {
                    "path": "api-preview.txt",
                    "content": "preview",
                    "expected_version_token": "missing",
                    "dry_run": True,
                },
            },
        )

    assert result.status_code == 200
    assert result.json()["success"] is True
    assert result.json()["operation"] == "file.write"
    assert result.json()["dry_run"] is True
    assert not (tmp_path / "api-preview.txt").exists()
