"""Synthetic policy checks for retaining v16 while integrating cloud plugins."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.permissions import PermissionDecision, permission_for_tool
from app.plugins.contracts import PluginCall
from app.plugins.handlers import execute_legacy_builtin
from app.plugins.registry import PLUGIN_LIBRARY
from app.tools import runtime_tools
from app.tools.outcomes import RuntimeToolOutcome
from app.tools.receipts import build_tool_receipt
from app.tools.registry import REGISTRY, ToolSpec, ToolValidationError, validate_arguments
from app.tools.spec import ToolSpec as CanonicalToolSpec, V16_SPECS
from app.workspace.file_locks import mutation_lock_paths


def _values(tmp_path, name: str, arguments: dict | None = None) -> dict:
    return {
        "workspace": str(tmp_path), "mode": "full", "name": name,
        "arguments": arguments or {}, "tool_call_id": "synthetic-call",
        "approved_actions": [], "approval_scope": "once",
        "conversation_id": 1, "task_id": "synthetic-task", "mcp_routes": {},
        "extension_routes": {}, "allow_local_mcp": False,
        "permission_fn": Mock(return_value=PermissionDecision(True, False)),
    }


def _offline(monkeypatch, enabled: bool = False) -> None:
    monkeypatch.setattr(runtime_tools, "local_only_policy", lambda: SimpleNamespace(enabled=enabled))


def test_v16_schemas_and_ownership_are_not_replaced_by_v8_modules():
    assert ToolSpec is CanonicalToolSpec
    assert runtime_tools.RuntimeToolOutcome is RuntimeToolOutcome
    canonical_names = {spec.name for spec in V16_SPECS}
    assert set(REGISTRY) == canonical_names | {"discover_tools", "transcribe_audio", "synthesize_speech"}
    for canonical in V16_SPECS:
        composed = REGISTRY[canonical.name]
        assert composed.openai() == canonical.openai()
        assert composed.risk == canonical.risk
        assert composed.version == canonical.version
        assert composed.idempotent == canonical.idempotent
        assert composed.rollback_support == canonical.rollback_support
        assert composed.verification_support == canonical.verification_support
        assert composed.interruptibility == canonical.interruptibility
        assert composed.concurrency_policy == canonical.concurrency_policy
        assert composed.selection_order == canonical.selection_order
        assert composed.plugin_id == PLUGIN_LIBRARY.owner(canonical.name).id
    assert [spec.name for spec in PLUGIN_LIBRARY.tool_specs() if spec.name in canonical_names] == [
        spec.name for spec in V16_SPECS
    ]


@pytest.mark.parametrize("name", [
    "delete_directory", "file_batch", "undo_file_batch", "artifact.docx.create",
    "artifact.pptx.edit", "artifact.pdf.extract", "vision.inspect_ui",
])
def test_local_v16_capabilities_remain_in_plugin_catalog(name):
    catalog = {tool["name"]: tool for plugin in PLUGIN_LIBRARY.catalog("synthetic-workspace") for tool in plugin["tools"]}
    assert name in catalog
    assert catalog[name]["input_schema"] == REGISTRY[name].openai()["function"]["parameters"]
    assert catalog[name]["version"] == REGISTRY[name].version
    assert catalog[name]["rollback_support"] == REGISTRY[name].rollback_support


@pytest.mark.parametrize("name", ["write_file", "delete_directory", "artifact.docx.edit", "artifact.pptx.edit"])
def test_version_bound_mutations_keep_required_token(name):
    assert "expected_version_token" in REGISTRY[name].required
    arguments = {field: "synthetic" for field in REGISTRY[name].required if field != "expected_version_token"}
    with pytest.raises(ToolValidationError, match="expected_version_token"):
        validate_arguments(name, arguments)


@pytest.mark.parametrize("name", ["transcribe_audio", "synthesize_speech"])
def test_speech_is_network_not_ordinary_filesystem(name):
    assert permission_for_tool(name) == "network.request"
    assert permission_for_tool(name, "plugin:voice") == "network.request"
    assert REGISTRY[name].risk == "critical"
    assert REGISTRY[name].idempotent is False


def test_discovery_is_read_only_and_speech_rollback_metadata_is_retained():
    assert permission_for_tool("discover_tools") == "filesystem.read"
    assert REGISTRY["synthesize_speech"].rollback_support is True
    assert REGISTRY["synthesize_speech"].verification_support == ("exists", "hash", "diff")
    assert mutation_lock_paths("synthesize_speech", {"path": "output.wav"}) == ("output.wav",)
    assert mutation_lock_paths("artifact.docx.create", {"path": "report.docx"}) == ("report.docx",)
    assert mutation_lock_paths("undo_file_batch", {}) == ("*",)


@pytest.mark.parametrize("name", ["transcribe_audio", "synthesize_speech"])
def test_local_only_blocks_speech_before_permission_or_provider(tmp_path, monkeypatch, name):
    _offline(monkeypatch, True)
    dispatch = AsyncMock(side_effect=AssertionError("provider must not run"))
    monkeypatch.setattr(PLUGIN_LIBRARY, "execute", dispatch)
    values = _values(tmp_path, name, {"path": "audio.wav"})
    result = asyncio.run(runtime_tools.execute_runtime_tool(**values))
    assert result.result["error_code"] == "offline_network_blocked"
    values["permission_fn"].assert_not_called()
    dispatch.assert_not_awaited()


def test_scope_denial_precedes_all_dispatch(tmp_path, monkeypatch):
    dispatch = AsyncMock(side_effect=AssertionError("out-of-scope tool must not run"))
    monkeypatch.setattr(PLUGIN_LIBRARY, "execute", dispatch)
    values = _values(tmp_path, "discover_tools", {"query": "files"})
    result = asyncio.run(runtime_tools.execute_runtime_tool(**values, available_tool_names=("read_file",)))
    assert result.result["error_code"] == "tool_scope_violation"
    values["permission_fn"].assert_not_called()
    dispatch.assert_not_awaited()


def test_core_file_alias_keeps_mature_file_operation_path(tmp_path, monkeypatch):
    _offline(monkeypatch)
    file_call = Mock(return_value={"success": True, "status": "ok", "data": {"path": "a.txt"}})
    dispatch = AsyncMock(side_effect=AssertionError("mature tools must not use old plugin adapters"))
    monkeypatch.setattr(runtime_tools, "execute_file_operation", file_call)
    monkeypatch.setattr(PLUGIN_LIBRARY, "execute", dispatch)
    result = asyncio.run(runtime_tools.execute_runtime_tool(**_values(tmp_path, "file.read", {"path": "a.txt"})))
    assert result.source == "builtin:file_core"
    file_call.assert_called_once()
    dispatch.assert_not_awaited()


def test_read_file_keeps_existing_sandbox_path(tmp_path, monkeypatch):
    _offline(monkeypatch)
    sandbox_call = Mock(return_value={"success": True, "status": "ok", "data": {"path": "a.txt"}})
    dispatch = AsyncMock(side_effect=AssertionError("existing tools must keep their sandbox adapter"))
    monkeypatch.setattr(runtime_tools, "execute_tool", sandbox_call)
    monkeypatch.setattr(PLUGIN_LIBRARY, "execute", dispatch)
    result = asyncio.run(runtime_tools.execute_runtime_tool(**_values(tmp_path, "read_file", {"path": "a.txt"})))
    assert result.source == "builtin"
    sandbox_call.assert_called_once()
    dispatch.assert_not_awaited()


def test_file_batch_keeps_mature_transaction_path(tmp_path, monkeypatch):
    _offline(monkeypatch)
    batch = Mock(return_value={"success": True, "status": "ok", "confirmed": True})
    dispatch = AsyncMock(side_effect=AssertionError("transactions must not use old plugin adapters"))
    monkeypatch.setattr(runtime_tools, "execute_file_batch", batch)
    monkeypatch.setattr(PLUGIN_LIBRARY, "execute", dispatch)
    result = asyncio.run(runtime_tools.execute_runtime_tool(**_values(tmp_path, "file_batch", {"operations": [
        {"operation": "file.write", "arguments": {"path": "a.txt", "content": "synthetic", "expected_version_token": "missing"}},
    ]})))
    assert result.source == "builtin:file_transaction"
    batch.assert_called_once()
    dispatch.assert_not_awaited()


def test_plugin_old_tool_uses_same_mature_dispatch(tmp_path, monkeypatch):
    execute = AsyncMock(return_value=RuntimeToolOutcome({"success": True, "status": "ok"}, False, "low", "builtin:file_core"))
    monkeypatch.setattr(runtime_tools, "execute_runtime_tool", execute)
    call = PluginCall(
        workspace=str(tmp_path), mode="ask", name="read_file", arguments={"path": "a.txt"},
        tool_call_id="synthetic-call", approved_actions=[], approval_scope="once",
        conversation_id=1, task_id="synthetic-task", permission_fn=Mock(),
        available_tool_names=("read_file",),
    )
    result = asyncio.run(execute_legacy_builtin(call))
    assert result.source == "builtin:file_core"
    assert execute.await_args.kwargs["mode"] == "ask"
    assert execute.await_args.kwargs["available_tool_names"] == ("read_file",)
    assert PLUGIN_LIBRARY.owner("read_file").handler is execute_legacy_builtin


def test_extension_critical_delegate_stays_denied_before_authorization(tmp_path, monkeypatch):
    from app.extensions.sdk import ExtensionToolRoute

    _offline(monkeypatch)
    route = ExtensionToolRoute("extension:synthetic:command", "synthetic", "1.0", "run_command", "medium", {}, "synthetic")
    values = _values(tmp_path, route.name, {"command": "synthetic.exe", "affected_paths": []})
    values["extension_routes"] = {route.name: route}
    result = asyncio.run(runtime_tools.execute_runtime_tool(**values))
    assert result.result["error_code"] == "invalid_extension_arguments"
    values["permission_fn"].assert_not_called()


def test_memory_deny_policy_stays_authoritative(tmp_path, monkeypatch):
    _offline(monkeypatch)
    memory_write = Mock(side_effect=AssertionError("denied memory write must not run"))
    monkeypatch.setattr(runtime_tools, "execute_memory_tool", memory_write)
    result = asyncio.run(runtime_tools.execute_runtime_tool(
        **_values(tmp_path, "remember_workspace", {"key": "synthetic", "content": "synthetic"}),
        memory_write_policy="deny",
    ))
    assert result.result["error_code"] == "memory_write_policy_denied"
    memory_write.assert_not_called()


def test_shared_outcome_retains_full_v16_receipt():
    receipt = build_tool_receipt("synthesize_speech", {
        "success": True, "status": "ok", "data": {
            "path": "output.wav", "change_id": "synthetic-change",
            "version_before": "missing", "version_after": "synthetic-version", "total_bytes": 48,
        },
    }, task_id="synthetic-task", tool_call_id="synthetic-call", risk_level="critical")
    initial = RuntimeToolOutcome({"success": True}, True, "critical", "plugin:voice")
    completed = replace(initial, receipt=receipt)
    assert completed.receipt.receipt_version == 2
    assert completed.receipt.task_id == "synthetic-task"
    assert completed.receipt.tool_call_id == "synthetic-call"
    assert completed.receipt.operation_kind == "mutation"
    assert completed.receipt.rollback["change_id"] == "synthetic-change"
