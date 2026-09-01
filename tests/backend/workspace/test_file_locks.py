import time
import uuid
from pathlib import Path

import pytest

from app.database import connect, init_db, now_iso
from app.workspace.file_locks import (
    FileLockConflict,
    acquire_file_locks,
    active_file_locks,
    mutation_lock_paths,
    release_file_locks,
    renew_file_locks,
)


def _task(workspace: Path) -> str:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "lock", str(workspace), "full", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "lock", stamp, stamp),
        )
    return task_id


def test_same_file_cannot_be_locked_by_two_tasks(tmp_path: Path) -> None:
    (tmp_path / "shared.txt").write_text("before", encoding="utf-8")
    first_task, second_task = _task(tmp_path), _task(tmp_path)
    first = acquire_file_locks(str(tmp_path), ("shared.txt",), holder_task_id=first_task, holder_agent_id=f"{first_task}:root")

    with pytest.raises(FileLockConflict):
        acquire_file_locks(str(tmp_path), ("shared.txt",), holder_task_id=second_task, holder_agent_id=f"{second_task}:root")

    release_file_locks(first)
    second = acquire_file_locks(str(tmp_path), ("shared.txt",), holder_task_id=second_task, holder_agent_id=f"{second_task}:root")
    assert second is not None
    release_file_locks(second)


def test_workspace_lock_conflicts_with_specific_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    first_task, second_task = _task(tmp_path), _task(tmp_path)
    workspace_lease = acquire_file_locks(str(tmp_path), ("*",), holder_task_id=first_task, holder_agent_id=f"{first_task}:root")
    with pytest.raises(FileLockConflict):
        acquire_file_locks(str(tmp_path), ("a.txt",), holder_task_id=second_task, holder_agent_id=f"{second_task}:root")
    release_file_locks(workspace_lease)


def test_task_ownership_does_not_depend_on_agent_identity(tmp_path: Path) -> None:
    (tmp_path / "shared.txt").write_text("before", encoding="utf-8")
    first_task, second_task = _task(tmp_path), _task(tmp_path)
    first = acquire_file_locks(
        str(tmp_path), ("shared.txt",), holder_task_id=first_task, holder_agent_id="fixed-agent"
    )
    with pytest.raises(FileLockConflict):
        acquire_file_locks(
            str(tmp_path), ("shared.txt",), holder_task_id=second_task, holder_agent_id="fixed-agent"
        )
    release_file_locks(first)


def test_same_task_can_reenter_and_renew_its_lease(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "shared.txt").write_text("before", encoding="utf-8")
    task_id = _task(tmp_path)
    monkeypatch.setattr("app.workspace.file_locks.settings.multi_agent_file_lock_seconds", 10)
    lease = acquire_file_locks(
        str(tmp_path), ("shared.txt",), holder_task_id=task_id, holder_agent_id="root"
    )
    assert lease is not None
    original_expiry = lease.expires_at
    monkeypatch.setattr("app.workspace.file_locks.time.time", lambda: original_expiry + 1)
    renewed = renew_file_locks(lease)
    assert renewed is not None and renewed.expires_at > original_expiry
    release_file_locks(renewed)


def test_lock_records_file_version_before_and_after(tmp_path: Path) -> None:
    target = tmp_path / "versioned.txt"
    target.write_text("before", encoding="utf-8")
    task_id = _task(tmp_path)
    lease = acquire_file_locks(str(tmp_path), ("versioned.txt",), holder_task_id=task_id, holder_agent_id=f"{task_id}:root")
    target.write_text("after", encoding="utf-8")
    release_file_locks(lease)

    with connect() as db:
        record = dict(db.execute("SELECT * FROM agent_file_locks WHERE holder_task_id=?", (task_id,)).fetchone())
    assert record["status"] == "released"
    assert record["version_before"] != record["version_after"]


def test_expired_lock_does_not_block_next_task(tmp_path: Path) -> None:
    (tmp_path / "expired.txt").write_text("value", encoding="utf-8")
    first_task, second_task = _task(tmp_path), _task(tmp_path)
    acquire_file_locks(str(tmp_path), ("expired.txt",), holder_task_id=first_task, holder_agent_id=f"{first_task}:root")
    with connect() as db:
        db.execute("UPDATE agent_file_locks SET expires_at=? WHERE holder_task_id=?", (time.time() - 1, first_task))
    second = acquire_file_locks(str(tmp_path), ("expired.txt",), holder_task_id=second_task, holder_agent_id=f"{second_task}:root")
    assert second is not None
    assert len(active_file_locks(str(tmp_path))) == 1
    release_file_locks(second)


def test_mutation_paths_cover_file_and_workspace_writes() -> None:
    assert mutation_lock_paths("write_file", {"path": "a.txt"}) == ("a.txt",)
    assert mutation_lock_paths("move_file", {"source": "a.txt", "destination": "b.txt"}) == ("a.txt", "b.txt")
    assert mutation_lock_paths(
        "run_command", {"command": "python", "affected_paths": []}
    ) == ("*",)
    assert mutation_lock_paths("remember_workspace", {"key": "x"}) == ()
