import asyncio
import json
import sys
import uuid
import warnings
import zipfile
from pathlib import Path

import pytest

from app.database import connect, now_iso
from app.sandbox import execute_command_async, execute_tool
from app.workspace.snapshots import (
    SnapshotError,
    create_security_snapshot,
    list_security_snapshots,
    preview_security_snapshot,
    restore_security_snapshot,
)


def _task(workspace: Path) -> tuple[int, str]:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as database:
        database.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "snapshot", str(workspace), "full", stamp, stamp),
        )
        database.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "snapshot", stamp, stamp),
        )
    return conversation_id, task_id


def test_snapshot_preview_and_restore_workspace(tmp_path: Path) -> None:
    conversation_id, task_id = _task(tmp_path)
    original = tmp_path / "a.txt"
    original.write_text("before", encoding="utf-8")
    snapshot = create_security_snapshot(str(tmp_path), reason="before_test", conversation_id=conversation_id, task_id=task_id)

    original.write_text("after", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")
    preview = preview_security_snapshot(str(tmp_path), snapshot["id"])
    assert preview["modified_since_snapshot"] == ["a.txt"]
    assert preview["added_since_snapshot"] == ["new.txt"]

    restored = restore_security_snapshot(str(tmp_path), snapshot["id"], conversation_id=conversation_id, task_id=task_id)
    assert original.read_text(encoding="utf-8") == "before"
    assert not (tmp_path / "new.txt").exists()
    assert restored["safety_snapshot_id"] != snapshot["id"]
    assert any(item["id"] == snapshot["id"] for item in list_security_snapshots(str(tmp_path), task_id))
    with connect() as database:
        row = database.execute("SELECT database_backup, restored_at FROM security_snapshots WHERE id=?", (snapshot["id"],)).fetchone()
    assert Path(row["database_backup"]).is_file()
    assert row["restored_at"] is not None


def test_command_requires_approval_and_creates_rollback_snapshot(tmp_path: Path) -> None:
    conversation_id, task_id = _task(tmp_path)
    arguments = {
        "command": sys.executable,
        "args": ["-c", "from pathlib import Path; Path('changed.txt').write_text('changed', encoding='utf-8')"],
        "timeout": 20,
    }
    pending = asyncio.run(execute_command_async(str(tmp_path), "full", arguments, conversation_id=conversation_id, task_id=task_id))
    assert pending["status"] == "confirmation_required"
    result = asyncio.run(execute_command_async(
        str(tmp_path),
        "full",
        arguments,
        [pending["approval_key"]],
        conversation_id=conversation_id,
        task_id=task_id,
    ))
    assert result["success"] is True
    snapshot_id = result["data"]["security_snapshot_id"]
    assert (tmp_path / "changed.txt").is_file()
    restore_security_snapshot(str(tmp_path), snapshot_id, conversation_id=conversation_id, task_id=task_id)
    assert not (tmp_path / "changed.txt").exists()


def test_snapshot_restore_tool_still_requires_critical_confirmation(tmp_path: Path) -> None:
    snapshot = create_security_snapshot(str(tmp_path), reason="tool_restore")
    pending = execute_tool(str(tmp_path), "full", "restore_security_snapshot", {"snapshot_id": snapshot["id"]})
    assert pending["status"] == "confirmation_required"


def test_snapshot_restore_rejects_tampered_or_duplicate_archive_members(tmp_path: Path) -> None:
    original = tmp_path / "a.txt"
    original.write_text("before", encoding="utf-8")
    snapshot = create_security_snapshot(str(tmp_path), reason="archive_integrity")
    with connect() as database:
        record = database.execute("SELECT manifest_path FROM security_snapshots WHERE id=?", (snapshot["id"],)).fetchone()
    archive_path = Path(record["manifest_path"]).parent / "workspace.zip"

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name: 'a.txt'")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("a.txt", b"tampered")
            archive.writestr("a.txt", b"duplicate")

    with pytest.raises(SnapshotError, match="archive does not match"):
        restore_security_snapshot(str(tmp_path), snapshot["id"])
    assert original.read_text(encoding="utf-8") == "before"


def test_incremental_snapshot_restores_only_requested_paths(tmp_path: Path) -> None:
    selected = tmp_path / "selected.txt"
    unselected = tmp_path / "unselected.txt"
    selected.write_text("selected-before", encoding="utf-8")
    unselected.write_text("unselected-before", encoding="utf-8")

    snapshot = create_security_snapshot(str(tmp_path), reason="incremental", paths=["selected.txt"])
    selected.write_text("selected-after", encoding="utf-8")
    unselected.write_text("unselected-after", encoding="utf-8")

    restored = restore_security_snapshot(str(tmp_path), snapshot["id"])

    assert restored["selection_mode"] == "incremental"
    assert selected.read_text(encoding="utf-8") == "selected-before"
    assert unselected.read_text(encoding="utf-8") == "unselected-after"


def test_incremental_snapshot_removes_requested_path_that_was_initially_missing(tmp_path: Path) -> None:
    snapshot = create_security_snapshot(str(tmp_path), reason="before_create", paths=["created.txt"])
    created = tmp_path / "created.txt"
    created.write_text("new", encoding="utf-8")

    restore_security_snapshot(str(tmp_path), snapshot["id"])

    assert not created.exists()


def test_controlled_snapshot_excludes_generated_secret_and_model_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('safe')\n", encoding="utf-8")
    generated_directories = [".git", "node_modules", "target", "build", "dist", "_internal", "cache", ".venv"]
    for directory in generated_directories:
        path = tmp_path / directory
        path.mkdir()
        (path / "excluded.bin").write_bytes(b"generated")
    for name in [".env", ".env.local", "private.pem", "signing.key", "credentials.json"]:
        (tmp_path / name).write_text("known-secret", encoding="utf-8")
    (tmp_path / "model.gguf").write_bytes(b"model")
    (tmp_path / "pytorch_model-00001.bin").write_bytes(b"model")

    snapshot = create_security_snapshot(str(tmp_path), reason="controlled")
    with connect() as database:
        record = database.execute("SELECT manifest_path FROM security_snapshots WHERE id=?", (snapshot["id"],)).fetchone()
    manifest_path = Path(record["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with zipfile.ZipFile(manifest_path.parent / "workspace.zip") as archive:
        archived = set(archive.namelist())

    assert manifest["selection"]["mode"] == "controlled_workspace"
    assert {item["path"] for item in manifest["files"]} == {"src/main.py"}
    assert manifest["files"][0]["version_token"].startswith("file:")
    assert archived == {"src/main.py"}
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "known-secret" not in serialized
    assert ".env" not in serialized
    assert "private.pem" not in serialized


def test_snapshot_exclusion_lists_are_configurable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.workspace.snapshots.settings.security_snapshot_generated_directory_exclusions", "custom-output")
    monkeypatch.setattr("app.workspace.snapshots.settings.security_snapshot_secret_exclusions", "vault.token")
    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "vault.token").write_text("secret", encoding="utf-8")
    (tmp_path / "custom-output").mkdir()
    (tmp_path / "custom-output" / "large.bin").write_bytes(b"generated")

    snapshot = create_security_snapshot(str(tmp_path), reason="configured-exclusions")
    with connect() as database:
        record = database.execute("SELECT manifest_path FROM security_snapshots WHERE id=?", (snapshot["id"],)).fetchone()
    manifest = json.loads(Path(record["manifest_path"]).read_text(encoding="utf-8"))

    assert {item["path"] for item in manifest["files"]} == {"keep.txt"}


def test_large_generated_and_model_files_do_not_block_command_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conversation_id, task_id = _task(tmp_path)
    generated = tmp_path / "node_modules"
    generated.mkdir()
    with (generated / "cache.bin").open("wb") as stream:
        stream.seek(2_000_000)
        stream.write(b"x")
    with (tmp_path / "model.gguf").open("wb") as stream:
        stream.seek(2_000_000)
        stream.write(b"x")
    monkeypatch.setattr("app.workspace.snapshots.settings.security_snapshot_max_bytes", 1_000_000)
    arguments = {
        "command": sys.executable,
        "args": ["-c", "from pathlib import Path; Path('changed.txt').write_text('changed', encoding='utf-8')"],
        "timeout": 20,
    }
    pending = asyncio.run(execute_command_async(str(tmp_path), "full", arguments, conversation_id=conversation_id, task_id=task_id))

    result = asyncio.run(execute_command_async(
        str(tmp_path),
        "full",
        arguments,
        [pending["approval_key"]],
        conversation_id=conversation_id,
        task_id=task_id,
    ))

    assert result["success"] is True
    restore_security_snapshot(str(tmp_path), result["security_snapshot_id"], conversation_id=conversation_id, task_id=task_id)
    assert not (tmp_path / "changed.txt").exists()
    assert (generated / "cache.bin").is_file()
    assert (tmp_path / "model.gguf").is_file()
