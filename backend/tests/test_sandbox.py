from pathlib import Path

import pytest

from app.sandbox import SandboxError, execute_tool, safe_path


def test_safe_path_rejects_workspace_escape(tmp_path: Path) -> None:
    with pytest.raises(SandboxError):
        safe_path(tmp_path, "../outside.txt")


def test_readonly_denies_write(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "readonly", "write_file", {"path": "a.txt", "content": "x"})
    assert result["status"] == "denied"
    assert not (tmp_path / "a.txt").exists()


def test_confirm_requires_approval_then_writes(tmp_path: Path) -> None:
    pending = execute_tool(str(tmp_path), "confirm", "write_file", {"path": "a.txt", "content": "hello"})
    assert pending["status"] == "confirmation_required"
    complete = execute_tool(str(tmp_path), "confirm", "write_file", {"path": "a.txt", "content": "hello"}, approved=True)
    assert complete["status"] == "ok"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"


def test_move_stays_inside_workspace(tmp_path: Path) -> None:
    (tmp_path / "from.txt").write_text("data", encoding="utf-8")
    result = execute_tool(str(tmp_path), "auto", "move_file", {"source": "from.txt", "destination": "nested/to.txt"})
    assert result["status"] == "ok"
    assert (tmp_path / "nested" / "to.txt").exists()


def test_search_files_finds_content(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nimportant needle\nomega", encoding="utf-8")
    result = execute_tool(str(tmp_path), "readonly", "search_files", {"query": "needle"})
    assert result["status"] == "ok"
    assert result["matches"][0]["path"] == "notes.txt"
    assert result["matches"][0]["line"] == 2


def test_command_requires_confirmation(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "confirm", "run_command", {"command": "git", "args": ["status"]})
    assert result["status"] == "confirmation_required"
