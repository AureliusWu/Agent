import asyncio
import sys
import uuid
from pathlib import Path

from app.database import connect, now_iso
from app.sandbox import execute_command_async, execute_tool
from app.workspace.snapshots import create_security_snapshot, list_security_snapshots, preview_security_snapshot, restore_security_snapshot


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
