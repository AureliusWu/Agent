from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest

from app import sandbox
from app.tools.file_operations import FileOperationRequest, execute_file_batch
from app.workspace import file_recovery
from app.workspace.recovery_inventory import list_recovery


def test_large_binary_can_be_planned_and_moved_without_text_decoding(tmp_path: Path, monkeypatch):
    source = tmp_path / "large.bin"
    with source.open("wb") as handle:
        for _ in range(21):
            handle.write(b"\xff" * (1024 * 1024))
    token = sandbox.file_version_token(source)
    monkeypatch.setattr(sandbox, "_read_text", lambda *_: (_ for _ in ()).throw(AssertionError("binary decoded")))
    result = execute_file_batch(str(tmp_path), [FileOperationRequest("file.move", {
        "source": "large.bin", "destination": "moved.bin", "expected_version_token": token,
        "expected_destination_version_token": "missing"})])
    assert result["success"], result
    assert not source.exists()
    assert sandbox.file_version_token(tmp_path / "moved.bin") == token


def test_large_text_rejects_edit_before_whole_read(tmp_path: Path, monkeypatch):
    path = tmp_path / "large.txt"
    with path.open("wb") as handle:
        handle.truncate(sandbox.MAX_ATOMIC_WRITE_BYTES + 1)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: (_ for _ in ()).throw(AssertionError("whole read")))
    with pytest.raises(sandbox.SandboxError, match="20 MiB"):
        sandbox._read_text(path)


@pytest.mark.parametrize("kind", ["low", "unknown"])
def test_backup_space_failure_does_not_modify_workspace(tmp_path: Path, monkeypatch, kind: str):
    path = tmp_path / "a.txt"
    path.write_text("before", encoding="utf-8")
    if kind == "low":
        usage = namedtuple("Usage", "total used free")
        monkeypatch.setattr(file_recovery.shutil, "disk_usage", lambda _: usage(100, 99, 1))
    else:
        monkeypatch.setattr(file_recovery.shutil, "disk_usage", lambda _: (_ for _ in ()).throw(OSError("unknown")))
    result = sandbox.execute_tool(str(tmp_path), "full", "write_file", {"path": "a.txt", "content": "after",
                                                                           "expected_version_token": sandbox.file_version_token(path)})
    assert result["error_code"] == ("recovery_space_insufficient" if kind == "low" else "recovery_space_unknown")
    assert path.read_text(encoding="utf-8") == "before"


def test_cancel_during_backup_copy_preserves_source_and_partial_backup(tmp_path: Path, monkeypatch):
    from app.runtime.cancellation import task_token, release_task_token

    source = tmp_path / "a.txt"
    source.write_text("before", encoding="utf-8")
    token = task_token("synthetic-cancel-copy")
    opened = Path.open
    copying = False

    def cancel_when_backup_opens(path, *args, **kwargs):
        nonlocal copying
        stream = opened(path, *args, **kwargs)
        if path.name == "0.bak" and "b" in str(args[0] if args else kwargs.get("mode", "")):
            copying = True
            token.cancel("synthetic backup copy cancellation")
        return stream

    monkeypatch.setattr(Path, "open", cancel_when_backup_opens)
    try:
        result = sandbox.execute_tool(str(tmp_path), "full", "write_file", {
            "path": "a.txt", "content": "after", "expected_version_token": sandbox.file_version_token(source)},
            task_id="synthetic-cancel-copy")
        assert copying, "cancellation must occur inside the backup copy phase"
        assert result["error_code"] == "recovery_scan_cancelled", result
        assert source.read_text(encoding="utf-8") == "before"
        assert len(list((tmp_path / ".agent-backups").glob("*/0.bak"))) == 1
    finally:
        release_task_token("synthetic-cancel-copy")


def test_backup_verification_uses_one_scan_budget(tmp_path: Path, monkeypatch):
    source = tmp_path / "a.txt"
    source.write_text("before", encoding="utf-8")
    original = sandbox._file_state
    budgets = []

    def collect_budget(path, budget=None):
        budgets.append(budget)
        return original(path, budget)

    monkeypatch.setattr(sandbox, "_file_state", collect_budget)
    sandbox._save_backup(tmp_path, "write_file", [source])
    assert len(budgets) >= 3
    assert all(budget is budgets[0] for budget in budgets), "backup and source verification must share cumulative limits"


def test_copy_budget_checks_cancel_after_partial_write(tmp_path: Path):
    source, destination = tmp_path / "source.bin", tmp_path / "partial.bak"
    source.write_bytes(b"x" * (3 * file_recovery.HASH_CHUNK_BYTES))
    budget = file_recovery.ScanBudget()
    budget.cancelled = lambda: destination.exists() and destination.stat().st_size >= file_recovery.HASH_CHUNK_BYTES
    with pytest.raises(file_recovery.RecoveryError, match="取消"):
        file_recovery.copy_bounded(source, destination, budget)
    assert destination.stat().st_size == file_recovery.HASH_CHUNK_BYTES
    assert source.stat().st_size == 3 * file_recovery.HASH_CHUNK_BYTES


def test_copy_budget_times_out_before_another_write(tmp_path: Path, monkeypatch):
    source, destination = tmp_path / "source.bin", tmp_path / "partial.bak"
    source.write_bytes(b"x" * (3 * file_recovery.HASH_CHUNK_BYTES))
    clock = [10.0]
    monkeypatch.setattr(file_recovery.time, "monotonic", lambda: clock[0])
    budget = file_recovery.ScanBudget(timeout_seconds=1)
    original_check = budget.check

    def advance_after_partial_write(**values):
        if destination.exists() and destination.stat().st_size:
            clock[0] += 2
        return original_check(**values)

    monkeypatch.setattr(budget, "check", advance_after_partial_write)
    with pytest.raises(file_recovery.RecoveryError, match="预算"):
        file_recovery.copy_bounded(source, destination, budget)
    assert destination.stat().st_size == file_recovery.HASH_CHUNK_BYTES
    assert source.stat().st_size == 3 * file_recovery.HASH_CHUNK_BYTES


def test_recovery_inventory_pages_and_shows_restored_retained_data(tmp_path: Path):
    first = sandbox.execute_tool(str(tmp_path), "full", "write_file", {"path": "a.txt", "content": "a", "expected_version_token": "missing"})
    second = sandbox.execute_tool(str(tmp_path), "full", "write_file", {"path": "b.txt", "content": "b", "expected_version_token": "missing"})
    assert sandbox.execute_tool(str(tmp_path), "full", "undo_file_change", {"change_id": second["change_id"]})["success"]
    page1 = list_recovery(str(tmp_path), limit=1)
    page2 = list_recovery(str(tmp_path), limit=1, offset=1)
    assert page1["total"] == page2["total"] == 2
    assert page1["items"][0]["status"] == "restored"
    assert page2["items"][0]["change_id"] == first["change_id"]
    assert page2["items"][0]["status"] == "available"
    assert page1["items"][0]["retained_bytes"] > 0
    assert page2["items"][0]["eligibility"] == "not_checked"


def test_cancelled_scan_stops_batch_before_new_effect(tmp_path: Path):
    from app.runtime.cancellation import task_token, release_task_token

    token = task_token("synthetic-cancel-scan")
    token.cancel("synthetic")
    try:
        result = execute_file_batch(str(tmp_path), [FileOperationRequest("file.write", {
            "path": "a.txt", "content": "a", "expected_version_token": "missing"})], task_id="synthetic-cancel-scan")
        assert result["cause_error_code"] == "recovery_scan_cancelled"
        assert not (tmp_path / "a.txt").exists()
    finally:
        release_task_token("synthetic-cancel-scan")


def test_plan_scan_time_excludes_idle_but_accumulates_active_work(tmp_path: Path, monkeypatch):
    from app.tools import batch_plan

    clock = [10.0]
    monkeypatch.setattr(batch_plan.time, "monotonic", lambda: clock[0])
    plan = batch_plan.FileBatchPlan(str(tmp_path))
    original = batch_plan.file_state

    def slow_scan(path, budget):
        clock[0] += 16
        return original(path, budget)

    monkeypatch.setattr(batch_plan, "file_state", slow_scan)
    assert plan.version(tmp_path / "missing") == "missing"
    clock[0] += 120  # waiting for a user is not scanner work
    with pytest.raises(file_recovery.RecoveryError, match="预算"):
        plan.version(tmp_path / "missing")
    assert plan._scan_seconds == 32


@pytest.mark.parametrize("text", ["first\r\nsecond\r\n", "first\nsecond\n", "first\rsecond\r", "", "no newline"])
def test_read_preserve_newlines_is_lossless_and_default_compatible(tmp_path: Path, text: str):
    from app.tools.registry import validate_arguments

    path = tmp_path / "文本.txt"
    path.write_bytes(text.encode("utf-8"))
    arguments = {"path": path.name, "preserve_newlines": True}
    validate_arguments("read_file", arguments)
    result = sandbox.execute_tool(str(tmp_path), "full", "read_file", arguments)
    assert result["success"], result
    assert result["content"] == text
    default = sandbox.execute_tool(str(tmp_path), "full", "read_file", {"path": path.name})
    assert default["content"] == "\n".join(text.splitlines())


def test_preserve_newlines_rejects_line_range_and_retains_truncation(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"a\r\nb\r\n")
    result = sandbox.execute_tool(str(tmp_path), "full", "read_file", {
        "path": "a.txt", "preserve_newlines": True, "start_line": 1})
    assert not result["success"]
    result = sandbox.execute_tool(str(tmp_path), "full", "read_file", {
        "path": "a.txt", "preserve_newlines": True, "max_chars": 3})
    assert result["truncated"] and result["content"] == "a\r\n"
