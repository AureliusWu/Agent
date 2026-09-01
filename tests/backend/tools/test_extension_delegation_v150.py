from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.extensions.sdk import ExtensionToolRoute
from app.permissions import authorize, set_permission_policy, revoke_permission_policy
from app.tools.runtime_tools import execute_runtime_tool


def route() -> ExtensionToolRoute:
    return ExtensionToolRoute("ext__fixture__write", "fixture", "1.0.0", "write_file",
                              "medium", {}, "fixture write")


def call(workspace: Path, mode: str, *, tokens=None, permission_fn=authorize):
    item = route()
    return asyncio.run(execute_runtime_tool(workspace=str(workspace), mode=mode,
        name=item.name, arguments={"path": "extension.txt", "content": "hello", "expected_version_token": "missing"},
        tool_call_id="extension-test", approved_actions=tokens or [], approval_scope="once",
        conversation_id=None, task_id=None, mcp_routes={}, extension_routes={item.name: item},
        allow_local_mcp=False, permission_fn=permission_fn)).result


@pytest.mark.parametrize("mode", ["ask", "agent", "full"])
def test_delegated_write_keeps_original_mode_and_one_approval(tmp_path: Path, mode: str, monkeypatch):
    from app.tools import runtime_tools
    original = runtime_tools.execute_tool
    modes = []

    def observe(workspace, actual_mode, *args, **kwargs):
        modes.append(actual_mode)
        return original(workspace, actual_mode, *args, **kwargs)

    monkeypatch.setattr(runtime_tools, "execute_tool", observe)
    result = call(tmp_path, mode)
    if result.get("status") == "confirmation_required":
        result = call(tmp_path, mode, tokens=[result["approval_key"]])
    assert result["success"] is True, result
    assert modes == [mode]


def test_delegate_denial_added_after_parent_approval_is_not_bypassed(tmp_path: Path):
    pending = call(tmp_path, "ask")
    policy = None

    def changed_policy(**kwargs):
        nonlocal policy
        decision = authorize(**kwargs)
        if decision.allowed:
            policy = set_permission_policy(permission="filesystem.write", effect="deny", scope="workspace",
                workspace=str(tmp_path), tool="write_file")
        return decision

    try:
        result = call(tmp_path, "ask", tokens=[pending["approval_key"]], permission_fn=changed_policy)
        assert result["error_code"] == "permission_denied", result
        assert not (tmp_path / "extension.txt").exists()
    finally:
        if policy:
            revoke_permission_policy(policy["id"])


def test_readonly_cannot_use_extension_approval(tmp_path: Path):
    pending = call(tmp_path, "ask")
    result = call(tmp_path, "readonly", tokens=[pending["approval_key"]])
    assert result["error_code"] == "read_only_mode"
    assert not (tmp_path / "extension.txt").exists()
