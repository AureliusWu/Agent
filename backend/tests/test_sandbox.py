import asyncio
from pathlib import Path
import subprocess
import sys

import pytest

from app.sandbox import SandboxError, execute_command_async, execute_tool, safe_path


def test_safe_path_rejects_workspace_escape(tmp_path: Path) -> None:
    with pytest.raises(SandboxError):
        safe_path(tmp_path, "../outside.txt")


def test_ask_mode_requires_approval_for_write(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "ask", "write_file", {"path": "a.txt", "content": "x"})
    assert result["status"] == "confirmation_required"
    assert not (tmp_path / "a.txt").exists()


def test_ask_requires_approval_then_writes(tmp_path: Path) -> None:
    pending = execute_tool(str(tmp_path), "ask", "write_file", {"path": "a.txt", "content": "hello"})
    assert pending["status"] == "confirmation_required"
    complete = execute_tool(str(tmp_path), "ask", "write_file", {"path": "a.txt", "content": "hello"}, approved=True)
    assert complete["status"] == "ok"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"


def test_move_stays_inside_workspace(tmp_path: Path) -> None:
    (tmp_path / "from.txt").write_text("data", encoding="utf-8")
    result = execute_tool(str(tmp_path), "full", "move_file", {"source": "from.txt", "destination": "nested/to.txt"})
    assert result["status"] == "ok"
    assert (tmp_path / "nested" / "to.txt").exists()


def test_search_files_finds_content(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nimportant needle\nomega", encoding="utf-8")
    result = execute_tool(str(tmp_path), "ask", "search_files", {"query": "needle"})
    assert result["status"] == "ok"
    assert result["matches"][0]["path"] == "notes.txt"
    assert result["matches"][0]["line"] == 2


def test_command_requires_confirmation(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "agent", "run_command", {"command": "git", "args": ["status"]})
    assert result["status"] == "confirmation_required"


def test_full_mode_still_confirms_commands(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "full", "run_command", {"command": "git", "args": ["status"]})
    assert result["status"] == "confirmation_required"


def test_agent_mode_approves_normal_file_changes(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "agent", "write_file", {"path": "a.txt", "content": "hello"})
    assert result["success"] is True
    assert (tmp_path / "a.txt").exists()


def test_full_mode_can_delete_and_undo(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("original", encoding="utf-8")
    deleted = execute_tool(str(tmp_path), "full", "delete_file", {"path": "a.txt"})
    assert deleted["success"] is True and not (tmp_path / "a.txt").exists()
    restored = execute_tool(str(tmp_path), "full", "undo_file_change", {})
    assert restored["success"] is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "original"


def test_unknown_tool_arguments_are_rejected(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "full", "read_file", {"path": "x", "dangerous": True})
    assert result["error_code"] == "invalid_arguments"


def test_symlink_escape_is_rejected_when_supported(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接")
    with pytest.raises(SandboxError):
        safe_path(tmp_path, "escape/secret.txt")


def test_command_timeout_is_standardized(tmp_path: Path, monkeypatch) -> None:
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("tool", 1)
    monkeypatch.setattr("app.sandbox.subprocess.run", timed_out)
    result = execute_tool(str(tmp_path), "full", "run_command", {"command": "git", "args": ["status"], "timeout": 1}, approved=True)
    assert result["error_code"] == "tool_timeout"
    assert result["retryable"] is True


def test_atomic_write_and_diff(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    preview = execute_tool(str(tmp_path), "full", "file_diff", {"path": "a.txt", "content": "new\n"})
    written = execute_tool(str(tmp_path), "full", "write_file", {"path": "a.txt", "content": "new\n"})
    assert "-old" in preview["diff"] and "+new" in preview["diff"]
    assert written["success"] is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new\n"


def test_undo_targets_most_recent_of_rapid_changes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("value", encoding="utf-8")
    execute_tool(str(tmp_path), "full", "move_file", {"source": "a.txt", "destination": "moved.txt"})
    execute_tool(str(tmp_path), "full", "delete_file", {"path": "moved.txt"})
    execute_tool(str(tmp_path), "full", "undo_file_change", {})
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "value"
    assert not (tmp_path / "a.txt").exists()


def test_undo_order_does_not_depend_on_manifest_timestamp(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("value", encoding="utf-8")
    execute_tool(str(tmp_path), "full", "move_file", {"source": "a.txt", "destination": "moved.txt"})
    execute_tool(str(tmp_path), "full", "delete_file", {"path": "moved.txt"})
    manifests = list((tmp_path / ".agent-backups").glob("*/manifest.json"))
    for manifest in manifests:
        manifest.touch()
    execute_tool(str(tmp_path), "full", "undo_file_change", {})
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "value"


def test_async_command_is_terminated_when_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        running = asyncio.create_task(execute_command_async(str(tmp_path), "full", {"command": sys.executable, "args": ["-c", "import time; time.sleep(30)"], "timeout": 60}, approved=True))
        await asyncio.sleep(0.3)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=3)

    asyncio.run(scenario())
