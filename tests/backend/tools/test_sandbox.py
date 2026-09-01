import asyncio
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from app.artifacts.store import read_artifact
from app.database import connect, now_iso
from app.sandbox import (
    SandboxError,
    _resolve_command_executable,
    execute_command_async,
    execute_tool,
    file_version_token,
    safe_path,
)


def _version(path: Path) -> str:
    return file_version_token(path)


def _task(workspace: Path) -> str:
    stamp = now_iso()
    task_id = uuid.uuid4().hex
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?)",
            ("sandbox test", str(workspace), "full", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, int(cursor.lastrowid), "running", "sandbox test", stamp, stamp),
        )
    return task_id


def test_safe_path_rejects_workspace_escape(tmp_path: Path) -> None:
    with pytest.raises(SandboxError):
        safe_path(tmp_path, "../outside.txt")


def test_bare_command_is_resolved_before_windows_process_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.sandbox.shutil.which", lambda command: f"resolved-{command}.exe")
    monkeypatch.setattr("app.sandbox.sys.executable", "python.exe")
    assert _resolve_command_executable("python") == "python.exe"
    assert _resolve_command_executable("pytest") == "resolved-pytest.exe"
    assert _resolve_command_executable(r"tools\\python.exe") == r"tools\\python.exe"


def test_packaged_runtime_resolves_python_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.sandbox.sys.executable", "agent-backend.exe")
    monkeypatch.setattr("app.sandbox.shutil.which", lambda command: f"resolved-{command}.exe")
    assert _resolve_command_executable("python") == "resolved-python.exe"


def test_ask_mode_requires_approval_for_write(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "ask", "write_file", {"path": "a.txt", "content": "x", "expected_version_token": "missing"})
    assert result["status"] == "confirmation_required"
    assert not (tmp_path / "a.txt").exists()


def test_ask_requires_approval_then_writes(tmp_path: Path) -> None:
    arguments = {"path": "a.txt", "content": "hello", "expected_version_token": "missing"}
    pending = execute_tool(str(tmp_path), "ask", "write_file", arguments)
    assert pending["status"] == "confirmation_required"
    complete = execute_tool(str(tmp_path), "ask", "write_file", arguments, [pending["approval_key"]])
    assert complete["status"] == "ok"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello"


def test_readonly_mode_hard_denies_write_without_approval_escape(tmp_path: Path) -> None:
    arguments = {"path": "a.txt", "content": "blocked", "expected_version_token": "missing"}

    denied = execute_tool(str(tmp_path), "readonly", "write_file", arguments)

    assert denied["status"] == "blocked"
    assert denied["error_code"] == "read_only_mode"
    assert "approval_key" not in denied
    assert not (tmp_path / "a.txt").exists()


def test_readonly_mode_hard_denies_delete_even_with_approval_token(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("keep", encoding="utf-8")
    arguments = {"path": "a.txt", "expected_version_token": _version(target)}

    denied = execute_tool(str(tmp_path), "readonly", "delete_file", arguments, ["untrusted-approval"])

    assert denied["status"] == "blocked"
    assert denied["error_code"] == "read_only_mode"
    assert target.read_text(encoding="utf-8") == "keep"


def test_readonly_mode_hard_denies_run_command_without_confirmation(tmp_path: Path) -> None:
    arguments = {
        "command": sys.executable,
        "args": ["-c", "print('must-not-run')"],
        "affected_paths": [],
    }

    synchronous = execute_tool(str(tmp_path), "readonly", "run_command", arguments, ["untrusted-approval"])
    denied = asyncio.run(execute_command_async(str(tmp_path), "readonly", arguments, ["untrusted-approval"]))

    assert synchronous["status"] == "blocked"
    assert synchronous["error_code"] == "read_only_mode"
    assert denied["status"] == "blocked"
    assert denied["error_code"] == "read_only_mode"
    assert "approval_key" not in denied


def test_move_stays_inside_workspace(tmp_path: Path) -> None:
    (tmp_path / "from.txt").write_text("data", encoding="utf-8")
    result = execute_tool(str(tmp_path), "full", "move_file", {"source": "from.txt", "destination": "nested/to.txt", "expected_version_token": _version(tmp_path / "from.txt"), "expected_destination_version_token": "missing"})
    assert result["status"] == "ok"
    assert (tmp_path / "nested" / "to.txt").exists()


def test_copy_preserves_source_and_creates_independent_version(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("copy me", encoding="utf-8")
    result = execute_tool(
        str(tmp_path),
        "agent",
        "copy_file",
        {
            "source": "source.txt",
            "destination": "copies/target.txt",
            "expected_version_token": _version(source),
            "expected_destination_version_token": "missing",
        },
    )
    target = tmp_path / "copies" / "target.txt"
    assert result["success"] is True
    assert source.read_bytes() == target.read_bytes()
    assert source.exists() and target.exists()
    target.write_text("independent edit", encoding="utf-8")
    assert source.read_text(encoding="utf-8") == "copy me"
    assert result["version_after"]["source"] == _version(source)


def test_nested_directory_move_is_complete_and_undoable(tmp_path: Path) -> None:
    source = tmp_path / "tree"
    (source / "nested").mkdir(parents=True)
    (source / "root.txt").write_text("root", encoding="utf-8")
    (source / "nested" / "leaf.txt").write_text("leaf", encoding="utf-8")
    result = execute_tool(
        str(tmp_path),
        "full",
        "move_file",
        {
            "source": "tree",
            "destination": "moved/tree",
            "expected_version_token": _version(source),
            "expected_destination_version_token": "missing",
        },
    )
    destination = tmp_path / "moved" / "tree"
    assert result["success"] is True
    assert not source.exists()
    assert (destination / "root.txt").read_text(encoding="utf-8") == "root"
    assert (destination / "nested" / "leaf.txt").read_text(encoding="utf-8") == "leaf"
    restored = execute_tool(str(tmp_path), "full", "undo_file_change", {})
    assert restored["success"] is True
    assert (source / "nested" / "leaf.txt").read_text(encoding="utf-8") == "leaf"
    assert not destination.exists()


def test_create_directory_is_idempotent_for_existing_directory(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    result = execute_tool(str(tmp_path), "full", "create_directory", {"path": "existing"})
    assert result["status"] == "ok"
    assert result["created"] is False
    assert existing.is_dir()


def test_search_files_finds_content(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nimportant needle\nomega", encoding="utf-8")
    result = execute_tool(str(tmp_path), "ask", "search_files", {"query": "needle"})
    assert result["status"] == "ok"
    assert result["matches"][0]["path"] == "notes.txt"
    assert result["matches"][0]["line"] == 2


def test_search_does_not_read_file_symlink_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir(); outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("outside-only-needle", encoding="utf-8")
    link = workspace / "linked-secret.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建文件符号链接")

    result = execute_tool(str(workspace), "readonly", "search_text", {"query": "outside-only-needle"})

    assert result["success"] is True
    assert result["matches"] == []


def test_search_does_not_descend_directory_symlink_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir(); outside.mkdir()
    (outside / "secret.txt").write_text("outside-dir-needle", encoding="utf-8")
    link = workspace / "linked-directory"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建目录符号链接")

    result = execute_tool(str(workspace), "readonly", "search_files", {"query": "outside-dir-needle"})

    assert result["success"] is True
    assert result["matches"] == []


def test_command_requires_confirmation(tmp_path: Path) -> None:
    result = execute_tool(
        str(tmp_path),
        "agent",
        "run_command",
        {"command": "git", "args": ["status"], "affected_paths": []},
    )
    assert result["status"] == "confirmation_required"


def test_full_mode_still_confirms_commands(tmp_path: Path) -> None:
    result = execute_tool(
        str(tmp_path),
        "full",
        "run_command",
        {"command": "git", "args": ["status"], "affected_paths": []},
    )
    assert result["status"] == "confirmation_required"


def test_command_requires_explicit_affected_paths_contract(tmp_path: Path) -> None:
    result = execute_tool(
        str(tmp_path), "full", "run_command", {"command": "git", "args": ["status"]}
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_arguments"
    assert "affected_paths" in result["error_message"]


def test_agent_mode_approves_normal_file_changes(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "agent", "write_file", {"path": "a.txt", "content": "hello", "expected_version_token": "missing"})
    assert result["success"] is True
    assert (tmp_path / "a.txt").exists()


def test_full_mode_can_delete_and_undo(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("original", encoding="utf-8")
    deleted = execute_tool(str(tmp_path), "full", "delete_file", {"path": "a.txt", "expected_version_token": _version(tmp_path / "a.txt")})
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
    arguments = {"command": "git", "args": ["status"], "affected_paths": [], "timeout": 1}
    pending = execute_tool(str(tmp_path), "full", "run_command", arguments)
    result = execute_tool(str(tmp_path), "full", "run_command", arguments, [pending["approval_key"]])
    assert result["error_code"] == "tool_timeout"
    assert result["retryable"] is True


def test_atomic_write_and_diff(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    preview = execute_tool(str(tmp_path), "full", "file_diff", {"path": "a.txt", "content": "new\n"})
    written = execute_tool(str(tmp_path), "full", "write_file", {"path": "a.txt", "content": "new\n", "expected_version_token": _version(tmp_path / "a.txt")})
    assert "-old" in preview["diff"] and "+new" in preview["diff"]
    assert written["success"] is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new\n"


def test_write_rejects_stale_version_token_without_overwriting(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("observed", encoding="utf-8")
    observed = _version(path)
    path.write_text("external change", encoding="utf-8")
    result = execute_tool(
        str(tmp_path),
        "full",
        "write_file",
        {"path": "a.txt", "content": "agent change", "expected_version_token": observed},
    )
    assert result["error_code"] == "version_conflict"
    assert result["retryable"] is True
    assert path.read_text(encoding="utf-8") == "external change"


def test_undo_targets_most_recent_of_rapid_changes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("value", encoding="utf-8")
    execute_tool(str(tmp_path), "full", "move_file", {"source": "a.txt", "destination": "moved.txt", "expected_version_token": _version(tmp_path / "a.txt"), "expected_destination_version_token": "missing"})
    execute_tool(str(tmp_path), "full", "delete_file", {"path": "moved.txt", "expected_version_token": _version(tmp_path / "moved.txt")})
    execute_tool(str(tmp_path), "full", "undo_file_change", {})
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "value"
    assert not (tmp_path / "a.txt").exists()


def test_undo_order_does_not_depend_on_manifest_timestamp(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("value", encoding="utf-8")
    execute_tool(str(tmp_path), "full", "move_file", {"source": "a.txt", "destination": "moved.txt", "expected_version_token": _version(tmp_path / "a.txt"), "expected_destination_version_token": "missing"})
    execute_tool(str(tmp_path), "full", "delete_file", {"path": "moved.txt", "expected_version_token": _version(tmp_path / "moved.txt")})
    manifests = list((tmp_path / ".agent-backups").glob("*/manifest.json"))
    for manifest in manifests:
        manifest.touch()
    execute_tool(str(tmp_path), "full", "undo_file_change", {})
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "value"


def test_async_command_is_terminated_when_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        arguments = {
            "command": sys.executable,
            "args": ["-c", "import time; time.sleep(30)"],
            "affected_paths": [],
            "timeout": 60,
        }
        pending = await execute_command_async(str(tmp_path), "full", arguments)
        running = asyncio.create_task(execute_command_async(str(tmp_path), "full", arguments, [pending["approval_key"]]))
        await asyncio.sleep(0.3)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=3)

    asyncio.run(scenario())


def test_command_cwd_cannot_escape_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        arguments = {
            "command": sys.executable,
            "args": ["-c", "print('unsafe')"],
            "affected_paths": [],
            "cwd": "..",
        }
        pending = await execute_command_async(str(tmp_path), "full", arguments)
        result = await execute_command_async(
            str(tmp_path), "full", arguments, [pending["approval_key"]]
        )
        assert result["success"] is False
        assert result["error_code"] == "tool_error"

    asyncio.run(scenario())


def test_high_risk_system_command_is_blocked_after_confirmation(tmp_path: Path) -> None:
    async def scenario() -> None:
        arguments = {"command": "format", "args": ["C:"], "affected_paths": []}
        pending = await execute_command_async(str(tmp_path), "full", arguments)
        result = await execute_command_async(
            str(tmp_path), "full", arguments, [pending["approval_key"]]
        )
        assert result["success"] is False
        assert result["error_code"] == "tool_error"

    asyncio.run(scenario())


def test_command_output_and_artifact_redact_environment_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        secret = "sk" + "-release_gate_environment_1234567890"
        monkeypatch.setenv("SIYI_TEST_SECRET", secret)
        task_id = _task(tmp_path)
        arguments = {
            "command": sys.executable,
            "args": [
                "-c",
                "import os; print((os.environ['SIYI_TEST_SECRET'] + '\\n') * 2000)",
            ],
            "affected_paths": [],
            "timeout": 30,
        }
        pending = await execute_command_async(
            str(tmp_path), "full", arguments, task_id=task_id
        )
        result = await execute_command_async(
            str(tmp_path),
            "full",
            arguments,
            [pending["approval_key"]],
            task_id=task_id,
        )
        assert result["success"] is True
        assert secret not in result["stdout"]
        assert "REDACTED" in result["stdout"]
        artifact = read_artifact(result["artifact_id"])
        assert secret not in artifact["content"]
        assert "REDACTED" in artifact["content"]

    asyncio.run(scenario())


def test_unc_and_drive_relative_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="UNC"):
        safe_path(tmp_path, r"\\server\share\secret.txt")
    if sys.platform == "win32":
        with pytest.raises(SandboxError, match="盘符相对"):
            safe_path(tmp_path, "C:secret.txt")


@pytest.mark.parametrize(
    "unsafe",
    [
        r"\\server\share\secret.txt",
        rf"\\?\{chr(67)}:\secret.txt",
        rf"\\.\{chr(67)}:\secret.txt",
    ],
)
def test_search_rejects_unc_and_windows_device_paths(tmp_path: Path, unsafe: str) -> None:
    result = execute_tool(str(tmp_path), "readonly", "search_files", {"query": "secret", "path": unsafe})

    assert result["success"] is False
    assert "UNC" in result["error_message"]


def test_junction_escape_is_rejected_on_windows(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Junction 仅适用于 Windows")
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir(); outside.mkdir()
    link = workspace / "escape"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False)
    if created.returncode != 0:
        pytest.skip("当前 Windows 环境无法创建 Junction")
    try:
        with pytest.raises(SandboxError):
            safe_path(workspace, "escape/secret.txt")
    finally:
        link.rmdir()


def test_search_does_not_descend_junction_outside_workspace_on_windows(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Junction 仅适用于 Windows")
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir(); outside.mkdir()
    (outside / "secret.txt").write_text("outside-junction-needle", encoding="utf-8")
    link = workspace / "escape"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False)
    if created.returncode != 0:
        pytest.skip("当前 Windows 环境无法创建 Junction")
    try:
        result = execute_tool(str(workspace), "readonly", "search_text", {"query": "outside-junction-needle"})
        assert result["success"] is True
        assert result["matches"] == []
    finally:
        link.rmdir()


def test_read_file_reports_encoding_size_and_total_lines(tmp_path: Path) -> None:
    (tmp_path / "gb.txt").write_bytes("第一行\r\n第二行\r\n".encode("gb18030"))
    result = execute_tool(str(tmp_path), "full", "read_file_range", {"path": "gb.txt", "start_line": 2, "end_line": 2, "encoding": "auto"})
    assert result["success"] is True
    assert result["content"] == "第二行"
    assert result["encoding"] == "gb18030"
    assert result["total_lines"] == 2
    assert result["file_size"] == (tmp_path / "gb.txt").stat().st_size


def test_large_file_is_read_by_range_and_full_output_is_stored_as_artifact(tmp_path: Path) -> None:
    lines = [f"line-{index:05d}-" + ("x" * 300) for index in range(1_000)]
    path = tmp_path / "large.log"
    path.write_text("\n".join(lines), encoding="utf-8")
    task_id = _task(tmp_path)
    result = execute_tool(
        str(tmp_path),
        "full",
        "read_file_range",
        {"path": "large.log", "start_line": 401, "end_line": 700, "encoding": "utf-8"},
        task_id=task_id,
        tool_call_id="large-read-call",
    )
    assert result["success"] is True
    assert result["start_line"] == 401 and result["end_line"] == 700
    assert result["total_lines"] == 1_000
    assert result["truncated"] is True
    assert result["artifact_id"]
    assert len(result["content"]) < result["file_size"]


def test_replace_text_preserves_crlf_and_returns_diff(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"alpha\r\nbeta\r\n")
    result = execute_tool(str(tmp_path), "agent", "replace_text", {"path": "a.txt", "old_text": "beta", "new_text": "gamma", "expected_version_token": _version(path)}, task_id="task-1", tool_call_id="call-1")
    assert result["success"] is True
    assert path.read_bytes() == b"alpha\r\ngamma\r\n"
    assert "-beta" in result["diff"] and "+gamma" in result["diff"]
    changes = execute_tool(str(tmp_path), "full", "list_file_changes", {"task_id": "task-1"})
    assert changes["changes"][0]["tool_call_id"] == "call-1"
    assert changes["changes"][0]["entries"][0]["before"]["sha256"]
    assert changes["changes"][0]["entries"][0]["after"]["sha256"]


def test_apply_patch_rejects_stale_content_and_applies_exact_hunk(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    stale = execute_tool(str(tmp_path), "agent", "apply_patch", {"path": "a.txt", "patch": "@@ -1,1 +1,1 @@\n-old\n+new", "expected_version_token": _version(path)})
    assert stale["success"] is False
    assert path.read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    applied = execute_tool(str(tmp_path), "agent", "apply_patch", {"path": "a.txt", "patch": "@@ -2,1 +2,1 @@\n-two\n+second", "expected_version_token": _version(path)})
    assert applied["success"] is True
    assert path.read_text(encoding="utf-8") == "one\nsecond\nthree\n"


def test_all_changes_for_task_can_be_undone(tmp_path: Path) -> None:
    execute_tool(str(tmp_path), "agent", "create_file", {"path": "a.txt", "content": "one"}, task_id="task-all", tool_call_id="one")
    execute_tool(str(tmp_path), "agent", "write_file", {"path": "a.txt", "content": "two", "expected_version_token": _version(tmp_path / "a.txt")}, task_id="task-all", tool_call_id="two")
    result = execute_tool(str(tmp_path), "full", "undo_task_changes", {"task_id": "task-all"})
    assert result["success"] is True
    assert result["undone"] == 2
    assert not (tmp_path / "a.txt").exists()


def test_search_regex_returns_context_and_honors_limit(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("before\nError 42\nafter\nError 43\n", encoding="utf-8")
    result = execute_tool(str(tmp_path), "full", "search_text", {"query": r"Error \d+", "regex": True, "context_lines": 1, "max_results": 1})
    assert result["truncated"] is True
    assert result["matches"][0]["line"] == 2
    assert result["matches"][0]["context"] == ["before", "Error 42", "after"]


def test_failed_atomic_replace_keeps_original_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "a.txt"
    path.write_text("original", encoding="utf-8")

    def fail_replace(*args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr("app.sandbox.os.replace", fail_replace)
    result = execute_tool(str(tmp_path), "agent", "write_file", {"path": "a.txt", "content": "changed", "expected_version_token": _version(path)})
    assert result["success"] is False
    assert path.read_text(encoding="utf-8") == "original"
    changes = execute_tool(str(tmp_path), "full", "list_file_changes", {})
    assert changes["changes"] == []
