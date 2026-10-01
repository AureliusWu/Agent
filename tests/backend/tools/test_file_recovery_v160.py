from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app import sandbox
from app.workspace import file_recovery


def write(root: Path, path: str, text: str, task: str = "v16-recovery") -> dict:
    result = sandbox.execute_tool(str(root), "full", "write_file", {
        "path": path, "content": text,
        "expected_version_token": sandbox.file_version_token(root / path),
    }, task_id=task)
    assert result["success"], result
    return result


def undo(root: Path, change: dict) -> dict:
    return sandbox.execute_tool(str(root), "full", "undo_file_change", {"change_id": change["change_id"]})


def manifest(root: Path, change: dict) -> Path:
    return root / ".agent-backups" / change["change_id"] / "manifest.json"


def test_verified_terminal_state_includes_successful_undo_and_detects_later_drift(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    change = write(tmp_path, "a.txt", "beta", task="undo-verification")
    restored = sandbox.execute_tool(str(tmp_path), "full", "undo_file_change", {"change_id": change["change_id"]},
                                    task_id="undo-verification", tool_call_id="restore-call")
    assert restored["success"], restored
    assert sandbox.verify_task_changes(str(tmp_path), "undo-verification")["status"] == "passed"
    (tmp_path / "a.txt").write_text("external", encoding="utf-8")
    assert sandbox.verify_task_changes(str(tmp_path), "undo-verification")["status"] == "failed"


def test_undo_actor_and_later_write_order_have_independent_verification(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    change = write(tmp_path, "a.txt", "beta", task="original-task")
    restored = sandbox.execute_tool(str(tmp_path), "full", "undo_file_change", {"change_id": change["change_id"]},
                                    task_id="restoring-task", tool_call_id="restore-call")
    assert restored["success"], restored
    assert sandbox.verify_task_changes(str(tmp_path), "restoring-task")["status"] == "passed"
    write(tmp_path, "a.txt", "gamma", task="restoring-task")
    assert sandbox.verify_task_changes(str(tmp_path), "restoring-task")["status"] == "passed"


def test_multiple_same_path_undo_events_verify_final_state(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    write(tmp_path, "a.txt", "beta", task="ordered-restore")
    write(tmp_path, "a.txt", "gamma", task="ordered-restore")
    result = sandbox.execute_tool(str(tmp_path), "full", "undo_task_changes", {"task_id": "ordered-restore"})
    assert result["success"], result
    assert sandbox.verify_task_changes(str(tmp_path), "ordered-restore")["status"] == "passed"


def test_external_edit_is_preserved_and_backup_retained(tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("original", encoding="utf-8")
    changed = write(tmp_path, "a.txt", "agent")
    path.write_text("external", encoding="utf-8")
    result = undo(tmp_path, changed)
    assert not result["success"]
    assert result["error_code"] == "recovery_conflict"
    assert path.read_text(encoding="utf-8") == "external"
    assert (manifest(tmp_path, changed).parent / "0.bak").read_text(encoding="utf-8") == "original"


def test_created_directory_with_new_child_is_not_deleted(tmp_path: Path):
    changed = sandbox.execute_tool(str(tmp_path), "full", "create_directory", {"path": "新 目录"})
    child = tmp_path / "新 目录" / "external.txt"
    child.write_text("keep", encoding="utf-8")
    assert not undo(tmp_path, changed)["success"]
    assert child.read_text(encoding="utf-8") == "keep"


def test_moved_directory_child_edit_is_detected_without_directory_mtime_change(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("old", encoding="utf-8")
    changed = sandbox.execute_tool(str(tmp_path), "full", "move_file", {
        "source": "source", "destination": "destination",
        "expected_version_token": sandbox.file_version_token(source),
        "expected_destination_version_token": "missing",
    })
    assert changed["success"], changed
    child = tmp_path / "destination" / "a.txt"
    child.write_text("new", encoding="utf-8")
    assert not undo(tmp_path, changed)["success"]
    assert child.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("damage", ["missing", "corrupt", "legacy", "type"])
def test_invalid_recovery_evidence_is_fail_closed(tmp_path: Path, damage: str):
    path = tmp_path / "a.txt"
    path.write_text("original", encoding="utf-8")
    changed = write(tmp_path, "a.txt", "agent")
    record = manifest(tmp_path, changed)
    if damage == "missing":
        (record.parent / "0.bak").unlink()
    elif damage == "corrupt":
        (record.parent / "0.bak").write_text("corrupt", encoding="utf-8")
    elif damage == "legacy":
        data = json.loads(record.read_text(encoding="utf-8"))
        data["entries"][0].pop("after", None)
        record.write_text(json.dumps(data), encoding="utf-8")
    else:
        path.unlink()
        path.mkdir()
        (path / "new.txt").write_text("external", encoding="utf-8")
    assert not undo(tmp_path, changed)["success"]
    assert record.exists()
    if damage != "type":
        assert path.read_text(encoding="utf-8") == "agent"
    else:
        assert (path / "new.txt").read_text(encoding="utf-8") == "external"


def test_task_undo_simulates_repeated_path_dependencies(tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("original", encoding="utf-8")
    write(tmp_path, "a.txt", "first")
    write(tmp_path, "a.txt", "second")
    result = sandbox.execute_tool(str(tmp_path), "full", "undo_task_changes", {"task_id": "v16-recovery"})
    assert result["success"], result
    assert result["undone"] == 2
    assert path.read_text(encoding="utf-8") == "original"


def test_task_preflight_checks_all_changes_before_restoring_any(tmp_path: Path):
    write(tmp_path, "first.txt", "first")
    write(tmp_path, "second.txt", "second")
    (tmp_path / "first.txt").write_text("external", encoding="utf-8")
    result = sandbox.execute_tool(str(tmp_path), "full", "undo_task_changes", {"task_id": "v16-recovery"})
    assert not result["success"]
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "external"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second"


def test_failed_internal_compensation_never_discards_backup(tmp_path: Path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("original", encoding="utf-8")
    change_id = sandbox._save_backup(tmp_path, "write_file", [path])
    path.write_text("uncertain", encoding="utf-8")
    monkeypatch.setattr(sandbox, "_undo_folder", lambda *_: (_ for _ in ()).throw(OSError("disk failed")))
    sandbox._rollback_backup(tmp_path, change_id)
    backup = tmp_path / ".agent-backups" / change_id
    assert (backup / "0.bak").read_text(encoding="utf-8") == "original"
    assert path.read_text(encoding="utf-8") == "uncertain"


def test_hash_metadata_does_not_materialize_whole_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "large.bin"
    path.write_bytes(b"x" * (3 * 1024 * 1024))
    monkeypatch.setattr(Path, "read_bytes", lambda *_: (_ for _ in ()).throw(AssertionError("unbounded read")))
    assert sandbox._file_state(path)["size"] == 3 * 1024 * 1024


def test_external_edit_after_preflight_is_not_overwritten(tmp_path: Path, monkeypatch):
    changed = write(tmp_path, "a.txt", "agent")
    original = file_recovery.preflight

    def edited_after_preflight(*args):
        plan = original(*args)
        (tmp_path / "a.txt").write_text("external", encoding="utf-8")
        return plan

    monkeypatch.setattr(file_recovery, "preflight", edited_after_preflight)
    result = undo(tmp_path, changed)
    assert result["error_code"] == "recovery_conflict"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "external"
    assert manifest(tmp_path, changed).exists()


def test_external_edit_after_first_restore_stops_next_step(tmp_path: Path, monkeypatch):
    first = write(tmp_path, "first.txt", "first")
    second = write(tmp_path, "second.txt", "second")
    original = file_recovery._restore_entry

    def edit_next_after_first(*args):
        result = original(*args)
        (tmp_path / "first.txt").write_text("external", encoding="utf-8")
        return result

    monkeypatch.setattr(file_recovery, "_restore_entry", edit_next_after_first)
    result = sandbox.execute_tool(str(tmp_path), "full", "undo_task_changes", {"task_id": "v16-recovery"})
    assert result["error_code"] == "recovery_conflict"
    assert result["restored"] == ["second.txt"]
    assert not (tmp_path / "second.txt").exists()
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "external"
    assert manifest(tmp_path, first).exists() and manifest(tmp_path, second).exists()


def test_external_edit_to_repeated_path_after_first_restore_is_not_adopted(tmp_path: Path, monkeypatch):
    write(tmp_path, "same.txt", "first")
    write(tmp_path, "same.txt", "second")
    original = file_recovery._restore_entry

    def edit_restored_path(*args):
        result = original(*args)
        (tmp_path / "same.txt").write_text("external", encoding="utf-8")
        return result

    monkeypatch.setattr(file_recovery, "_restore_entry", edit_restored_path)
    result = sandbox.execute_tool(str(tmp_path), "full", "undo_task_changes", {"task_id": "v16-recovery"})
    assert result["error_code"] == "recovery_conflict"
    assert result["restored"] == ["same.txt"]
    assert (tmp_path / "same.txt").read_text(encoding="utf-8") == "external"


@pytest.mark.parametrize("failure", ["copy", "rename", "second"])
def test_restore_io_failure_preserves_originals_and_reports_partial_progress(tmp_path: Path, monkeypatch, failure: str):
    (tmp_path / "a.txt").write_text("original", encoding="utf-8")
    changed = write(tmp_path, "a.txt", "agent")
    if failure == "second":
        write(tmp_path, "b.txt", "second")
    if failure == "copy":
        monkeypatch.setattr(file_recovery.shutil, "copy2", lambda *_args, **_kw: (_ for _ in ()).throw(OSError("disk full")))
    else:
        original = file_recovery._rename_no_replace

        def locked(source, target):
            if Path(source).name == "a.txt":
                raise PermissionError("file locked")
            return original(source, target)

        monkeypatch.setattr(file_recovery, "_rename_no_replace", locked)
    result = (sandbox.execute_tool(str(tmp_path), "full", "undo_task_changes", {"task_id": "v16-recovery"})
              if failure == "second" else undo(tmp_path, changed))
    assert result["error_code"] == "recovery_io_error"
    assert result["restored"] == (["b.txt"] if failure == "second" else [])
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "agent"
    assert (manifest(tmp_path, changed).parent / "0.bak").read_text(encoding="utf-8") == "original"
    assert json.loads(manifest(tmp_path, changed).read_text(encoding="utf-8"))["recovery"]["status"] == "needs_attention"


def test_backup_changed_after_preflight_does_not_remove_current_file(tmp_path: Path, monkeypatch):
    (tmp_path / "a.txt").write_text("original", encoding="utf-8")
    changed = write(tmp_path, "a.txt", "agent")
    original = file_recovery.preflight

    def corrupt(*args):
        result = original(*args)
        (manifest(tmp_path, changed).parent / "0.bak").write_text("corrupt", encoding="utf-8")
        return result

    monkeypatch.setattr(file_recovery, "preflight", corrupt)
    assert undo(tmp_path, changed)["error_code"] == "recovery_backup_invalid"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "agent"


def test_new_external_target_during_commit_is_not_overwritten(tmp_path: Path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("original", encoding="utf-8")
    changed = write(tmp_path, "a.txt", "agent")
    original = file_recovery._rename_no_replace

    def create_after_displacement(source, destination):
        original(source, destination)
        if Path(source) == path:
            path.write_text("new external", encoding="utf-8")

    monkeypatch.setattr(file_recovery, "_rename_no_replace", create_after_displacement)
    assert not undo(tmp_path, changed)["success"]
    assert path.read_text(encoding="utf-8") == "new external"
    retained = manifest(tmp_path, changed).parent
    assert (retained / "0.bak").read_text(encoding="utf-8") == "original"
    assert any(p.read_text(encoding="utf-8") == "agent" for p in retained.glob("recovery-displaced-*"))


def test_task_created_parent_and_child_undo_in_reverse(tmp_path: Path):
    created = sandbox.execute_tool(str(tmp_path), "full", "create_directory", {"path": "parent"}, task_id="v16-recovery")
    assert created["success"]
    write(tmp_path, "parent/child.txt", "child")
    result = sandbox.execute_tool(str(tmp_path), "full", "undo_task_changes", {"task_id": "v16-recovery"})
    assert result["success"], result
    assert not (tmp_path / "parent").exists()


def test_readonly_cannot_undo_even_with_valid_backup(tmp_path: Path):
    changed = write(tmp_path, "a.txt", "agent")
    result = sandbox.execute_tool(str(tmp_path), "readonly", "undo_file_change", {"change_id": changed["change_id"]})
    assert not result.get("success", False)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "agent"


def test_foreign_task_lock_blocks_undo(tmp_path: Path):
    from app.database import connect, now_iso
    from app.workspace.file_locks import acquire_file_locks, release_file_locks

    changed = write(tmp_path, "a.txt", "agent")
    task_id = uuid.uuid4().hex
    with connect() as db:
        stamp = now_iso()
        cursor = db.execute("INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                            ("recovery-test", str(tmp_path), "full", stamp, stamp))
        db.execute("INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                   (task_id, cursor.lastrowid, "running", "synthetic", stamp, stamp))
    lease = acquire_file_locks(str(tmp_path), ["a.txt"], holder_task_id=task_id, holder_agent_id="foreign")
    try:
        result = undo(tmp_path, changed)
        assert result["error_code"] == "recovery_locked"
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "agent"
    finally:
        release_file_locks(lease)


def test_directory_evidence_budget_and_cancellation_fail_closed(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(file_recovery.RecoveryError, match="预算"):
        file_recovery.file_state(tmp_path, file_recovery.ScanBudget(max_entries=1))
    with pytest.raises(file_recovery.RecoveryError, match="取消"):
        file_recovery.file_state(tmp_path, file_recovery.ScanBudget(cancelled=lambda: True))


def test_permission_changed_after_preflight_stops_recovery(tmp_path: Path, monkeypatch):
    from app.database import connect, now_iso

    changed = write(tmp_path, "a.txt", "agent")
    with connect() as db:
        stamp = now_iso()
        cursor = db.execute("INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                            ("recovery-authority", str(tmp_path), "full", stamp, stamp))
        conversation_id = cursor.lastrowid
    original = file_recovery.preflight

    def revoke(*args):
        plan = original(*args)
        with connect() as db:
            db.execute("UPDATE conversations SET permission_mode='readonly' WHERE id=?", (conversation_id,))
        return plan

    monkeypatch.setattr(file_recovery, "preflight", revoke)
    result = sandbox.execute_tool(str(tmp_path), "full", "undo_file_change", {"change_id": changed["change_id"]},
                                  conversation_id=conversation_id)
    assert result["error_code"] == "recovery_authority_changed"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "agent"


def test_truncated_copy_never_starts_workspace_mutation(tmp_path: Path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("original", encoding="utf-8")
    monkeypatch.setattr(sandbox, "copy_bounded", lambda _source, dest, _budget: Path(dest).write_text("partial", encoding="utf-8"))
    result = sandbox.execute_tool(str(tmp_path), "full", "write_file", {
        "path": "a.txt", "content": "agent", "expected_version_token": sandbox.file_version_token(path)})
    assert result["error_code"] == "recovery_backup_invalid"
    assert path.read_text(encoding="utf-8") == "original"


def test_directory_token_tracks_child_content_not_only_parent_timestamp(tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("first", encoding="utf-8")
    first = sandbox.file_version_token(tmp_path)
    path.write_text("other", encoding="utf-8")
    assert sandbox.file_version_token(tmp_path) != first


def test_windows_parent_pin_blocks_parent_rename_but_allows_child_commit(tmp_path: Path):
    import os

    parent = tmp_path / "parent"
    parent.mkdir()
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    (parent / "a.txt").write_text("current", encoding="utf-8")
    with file_recovery._pinned_directories(parent, recovery):
        with pytest.raises(OSError):
            parent.rename(tmp_path / "moved-parent")
        file_recovery._rename_no_replace(parent / "a.txt", recovery / "displaced.txt")
        (parent / "b.txt").write_text("normal child write", encoding="utf-8")
    assert (recovery / "displaced.txt").read_text(encoding="utf-8") == "current"


def test_native_recovery_supports_chinese_and_non_bmp_names(tmp_path: Path):
    path = "中文 空格😀.txt"
    (tmp_path / path).write_text("original", encoding="utf-8")
    changed = write(tmp_path, path, "agent")
    result = undo(tmp_path, changed)
    assert result["success"], result
    assert (tmp_path / path).read_text(encoding="utf-8") == "original"
