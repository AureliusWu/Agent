"""Typed local edit failures; no model, process, MCP transport or credentials."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.permissions import PermissionDecision
from app.runtime.executor import ExecutorToolCall, LocalWindowsExecutor
from app.sandbox import SandboxError, _list_changes, _undo, execute_tool, file_version_token, safe_path
from app.tools.outcomes import RuntimeToolOutcome
from app.tools.receipts import build_tool_receipt


def _allow(**_kwargs: Any) -> PermissionDecision:
    return PermissionDecision(allowed=True, confirmed=False)


def _arguments(path: Path, *, relative: str | None = None) -> dict[str, Any]:
    return {
        "path": relative or path.name,
        "old_text": "private-old-text-not-in-file",
        "new_text": "private-new-text-not-in-result",
        "expected_count": 1,
        "expected_version_token": file_version_token(path),
    }


def _call(workspace: Path, arguments: dict[str, Any]) -> ExecutorToolCall:
    return ExecutorToolCall(
        workspace=str(workspace), mode="agent", name="replace_text",
        arguments=arguments, tool_call_id="edit-precondition-call",
        approved_actions=[], approval_scope="once", conversation_id=1,
        task_id="edit-precondition-task", mcp_routes={},
        permission_fn=_allow, available_tool_names=("replace_text", "write_file"),
    )


@pytest.mark.parametrize("content,old_text,expected,actual", [
    ("original", "absent", 1, 0),
    ("twice twice", "twice", 1, 2),
    ("twice twice", "twice", 3, 2),
])
def test_match_count_failure_is_typed_and_never_starts_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    content: str, old_text: str, expected: int, actual: int,
) -> None:
    path = tmp_path / "note.txt"
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()
    version = file_version_token(path)

    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Edit precondition must fail before backup or write")

    monkeypatch.setattr("app.sandbox._save_backup", unexpected)
    monkeypatch.setattr("app.sandbox._atomic_write", unexpected)
    arguments = {**_arguments(path), "old_text": old_text, "expected_count": expected}
    result = execute_tool(str(tmp_path), "agent", "replace_text", arguments, permission_fn=_allow)

    assert result["success"] is False and result["status"] == "error"
    assert result["error_code"] == "edit_match_precondition"
    assert result["retryable"] is False
    assert result["version_before"] == result["data"]["version_before"] == version
    assert result["edit_precondition"] == result["data"]["edit_precondition"] == {
        "schema_version": 1, "kind": "replace_text_match_count", "path": "note.txt",
        "expected_count": expected, "actual_count": actual, "side_effect": "not_started",
    }
    assert "path" not in result and "path" not in result["data"]
    assert "change_id" not in result and "version_after" not in result
    assert path.read_bytes() == before and file_version_token(path) == version
    assert not (tmp_path / ".agent-backups").exists()


def test_precondition_contains_only_canonical_relative_metadata(tmp_path: Path) -> None:
    folder = tmp_path / "资料"
    folder.mkdir()
    path = folder / "例.txt"
    path.write_text("original", encoding="utf-8")
    arguments = _arguments(path, relative="资料/../资料/例.txt")

    result = execute_tool(str(tmp_path), "agent", "replace_text", arguments, permission_fn=_allow)

    assert result["edit_precondition"]["path"] == "资料/例.txt"
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert arguments["old_text"] not in serialized
    assert arguments["new_text"] not in serialized
    assert "old_text" not in serialized and "new_text" not in serialized


def test_failure_receipt_keeps_version_but_does_not_claim_changed_files(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("original", encoding="utf-8")
    arguments = _arguments(path)
    result = execute_tool(str(tmp_path), "agent", "replace_text", arguments, permission_fn=_allow)

    receipt = build_tool_receipt(
        "replace_text", result, task_id="edit-task", tool_call_id="edit-call",
        arguments=arguments, permission_decision="evaluated", risk_level="medium",
    )

    assert receipt.error_code == "edit_match_precondition"
    assert receipt.standard_status == "FAILED" and receipt.success is False
    assert receipt.changed_files == () and receipt.observed_files == ()
    assert receipt.version_before == file_version_token(path)
    assert receipt.version_after is None and receipt.change_id is None
    assert receipt.task_id == "edit-task" and receipt.tool_call_id == "edit-call"
    assert receipt.permission_decision == "evaluated" and receipt.error_fingerprint


def test_normal_replace_keeps_crlf_and_real_change_receipt_data(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"alpha\r\nbeta\r\n")
    before = file_version_token(path)

    result = execute_tool(
        str(tmp_path), "agent", "replace_text",
        {**_arguments(path), "old_text": "beta", "new_text": "gamma"},
        task_id="edit-task", tool_call_id="normal-edit", permission_fn=_allow,
    )

    assert result["success"] is True and result["error_code"] is None
    assert "edit_precondition" not in result
    assert path.read_bytes() == b"alpha\r\ngamma\r\n"
    assert result["version_before"] == before
    assert result["version_after"] == file_version_token(path)
    assert result["change_id"] and result["path"] == "note.txt"


@pytest.mark.parametrize("failure,expected_code", [
    ("version", "version_conflict"),
    ("escape", "tool_error"),
    ("arguments", "invalid_arguments"),
    ("io", "io_error"),
    ("sandbox_same_message", "tool_error"),
])
def test_other_failures_are_not_reclassified_by_message_or_tool_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, expected_code: str,
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("original", encoding="utf-8")
    arguments = _arguments(path)
    if failure == "version":
        arguments["expected_version_token"] = "stale"
    elif failure == "escape":
        arguments["path"] = "../outside.txt"
    elif failure == "arguments":
        del arguments["old_text"]
    elif failure in {"io", "sandbox_same_message"}:
        def fail_read(*_args: Any, **_kwargs: Any) -> Any:
            error = OSError if failure == "io" else SandboxError
            raise error("预期匹配 1 次，实际匹配 0 次")
        monkeypatch.setattr("app.sandbox._read_text", fail_read)

    result = execute_tool(str(tmp_path), "agent", "replace_text", arguments, permission_fn=_allow)

    assert result["success"] is False and result["error_code"] == expected_code
    assert "edit_precondition" not in result and "edit_precondition" not in result["data"]
    assert path.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / ".agent-backups").exists()


@pytest.mark.parametrize("mode,expected_code", [("ask", "confirmation_required"), ("readonly", "read_only_mode")])
def test_authorization_precedes_typed_edit_precondition(tmp_path: Path, mode: str, expected_code: str) -> None:
    path = tmp_path / "note.txt"
    path.write_text("original", encoding="utf-8")

    result = execute_tool(str(tmp_path), mode, "replace_text", _arguments(path))

    assert result["success"] is False
    if mode == "ask":
        assert result["status"] == "confirmation_required"
        assert result.get("error_code") != "edit_match_precondition"
    else:
        assert result["error_code"] == expected_code
    assert "edit_precondition" not in result
    assert path.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / ".agent-backups").exists()


def test_real_executor_preserves_bound_builtin_failure_and_empty_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("original", encoding="utf-8")

    async def quiet_hooks(_event: Any) -> list[Any]:
        return []

    monkeypatch.setattr("app.runtime.executor.run_hooks", quiet_hooks)
    call = _call(tmp_path, _arguments(path))
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(call))

    assert outcome.source == "builtin" and outcome.risk == "medium"
    assert outcome.result["error_code"] == "edit_match_precondition"
    assert outcome.receipt is not None and outcome.receipt.success is False
    assert outcome.receipt.task_id == call.task_id
    assert outcome.receipt.tool_call_id == call.tool_call_id
    assert outcome.receipt.changed_files == ()
    assert outcome.receipt.version_before == file_version_token(path)
    assert outcome.result["receipt"] == outcome.receipt.as_dict()


@pytest.mark.parametrize("source", ["mcp", "extension:synthetic", "plugin:synthetic"])
def test_external_forgery_does_not_acquire_builtin_routing_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("original", encoding="utf-8")
    arguments = _arguments(path)
    forged = execute_tool(str(tmp_path), "agent", "replace_text", arguments, permission_fn=_allow)
    forged["source"] = "builtin"
    forged["plugin"] = {"id": "builtin.writing", "version": "forged"}

    async def external_dispatch(**_kwargs: Any) -> RuntimeToolOutcome:
        return RuntimeToolOutcome(copy.deepcopy(forged), False, "medium", source)

    async def quiet_hooks(_event: Any) -> list[Any]:
        return []

    monkeypatch.setattr("app.runtime.executor.execute_runtime_tool", external_dispatch)
    monkeypatch.setattr("app.runtime.executor.run_hooks", quiet_hooks)
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(_call(tmp_path, arguments)))

    # The Verifier must use this trusted envelope/DB source, never the result's
    # source/plugin claims or a matching typed code alone.
    assert outcome.source == source and outcome.source != "builtin"
    assert outcome.result["error_code"] == "edit_match_precondition"
    assert outcome.receipt is not None and outcome.receipt.changed_files == ()
    assert path.read_text(encoding="utf-8") == "original"


def _internal_target(workspace: Path, form: str) -> tuple[Path, str]:
    directory = ".AgEnT-BaCkUpS" if form.startswith("mixed_") else ".agent-backups"
    backup = workspace / directory
    backup.mkdir(exist_ok=True)
    target = backup / "entry.txt"
    target.write_text("private-backup-fixture", encoding="utf-8")
    (workspace / "ordinary").mkdir(exist_ok=True)
    if form.endswith("absolute"):
        requested = str(target.resolve())
    elif form.endswith("dotdot"):
        requested = f"ordinary/../{directory}/entry.txt"
    elif form == "dot_prefix":
        requested = f"./{directory}/entry.txt"
    else:
        requested = f"{directory}/entry.txt"
    return target, requested


@pytest.mark.parametrize("form", ["raw", "dot_prefix", "absolute", "dotdot", "mixed_absolute", "mixed_dotdot"])
def test_safe_path_rejects_canonical_internal_backup_targets_without_symlinks(tmp_path: Path, form: str) -> None:
    _target, requested = _internal_target(tmp_path, form)

    with pytest.raises(SandboxError, match="内部备份目录不能直接操作"):
        safe_path(tmp_path.resolve(), requested, must_exist=True)


@pytest.mark.parametrize("form", ["absolute", "dotdot", "mixed_absolute", "mixed_dotdot"])
@pytest.mark.parametrize("tool", ["read_file", "write_file", "replace_text"])
def test_user_tools_cannot_read_or_write_canonical_internal_backups(
    tmp_path: Path, form: str, tool: str,
) -> None:
    target, requested = _internal_target(tmp_path, form)
    before = target.read_bytes()
    if tool == "read_file":
        arguments = {"path": requested}
    elif tool == "write_file":
        arguments = {"path": requested, "content": "must-not-write", "expected_version_token": file_version_token(target)}
    else:
        arguments = _arguments(target, relative=requested)

    result = execute_tool(str(tmp_path), "full", tool, arguments, permission_fn=_allow)

    assert result["success"] is False and result["error_code"] == "tool_error"
    assert result["error_message"] == "内部备份目录不能直接操作"
    assert "content" not in result and "edit_precondition" not in result
    assert target.read_bytes() == before
    assert sorted(item.name for item in target.parent.iterdir()) == ["entry.txt"]


def test_canonical_backup_guard_does_not_block_internal_history_and_undo(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("original", encoding="utf-8")
    result = execute_tool(
        str(tmp_path), "agent", "write_file",
        {"path": "note.txt", "content": "changed", "expected_version_token": file_version_token(path)},
        task_id="internal-undo-task", tool_call_id="internal-undo-write", permission_fn=_allow,
    )
    assert result["success"] is True and result["change_id"]
    root = tmp_path.resolve()
    history = _list_changes(root, "internal-undo-task")
    assert len(history) == 1 and history[0]["id"] == result["change_id"]
    assert history[0]["entries"][0]["after"]["sha256"]

    restored = _undo(root, result["change_id"])

    assert restored["change_id"] == result["change_id"]
    assert path.read_text(encoding="utf-8") == "original"
    retained = _list_changes(root, "internal-undo-task", include_restored=True)
    assert len(retained) == 1 and retained[0]["recovery"]["status"] == "restored"


def test_canonical_backup_guard_does_not_block_unrelated_similar_names(tmp_path: Path) -> None:
    folder = tmp_path / ".agent-backups-other"
    folder.mkdir()
    path = folder / "note.txt"
    path.write_text("ordinary", encoding="utf-8")

    assert safe_path(tmp_path.resolve(), ".agent-backups-other/note.txt", must_exist=True) == path.resolve()
    result = execute_tool(
        str(tmp_path), "readonly", "read_file", {"path": ".agent-backups-other/note.txt"},
    )
    assert result["success"] is True and result["content"] == "ordinary"
