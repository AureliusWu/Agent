from __future__ import annotations

import asyncio

import pytest

from app.permissions import permission_for_tool
from app.personality.agent_profiles import BUILTIN_PROFILES
from app.runtime.executor import LocalWindowsExecutor
from app.runtime.recovery import MUTATION_TOOLS as RECOVERY_MUTATIONS
from app.runtime.repair import MUTATION_TOOLS as REPAIR_MUTATIONS
from app.sandbox import (
    FileVersionError,
    file_version_token,
    recover_file_operation,
    write_workspace_binaries,
    write_workspace_binary,
)
from app.tools.receipts import MUTATION_TOOLS as RECEIPT_MUTATIONS
from app.tools.receipts import build_tool_receipt
from app.tools.registry import REGISTRY, ToolValidationError, validate_arguments
from app.tools.skills import BUILTIN_SKILLS, _required_permissions
from app.workspace.file_locks import mutation_lock_paths


ARTIFACT_TOOLS = {
    "artifact.markdown.create",
    "artifact.docx.create",
    "artifact.docx.edit",
    "artifact.pdf.create",
    "artifact.pdf.merge",
    "artifact.pdf.extract",
    "artifact.pptx.create",
    "artifact.pptx.edit",
    "artifact.render",
    "artifact.validate",
}
ARTIFACT_MUTATIONS = ARTIFACT_TOOLS - {
    "artifact.pdf.extract",
    "artifact.validate",
}


def test_artifact_tools_are_registered_with_permission_and_runtime_boundaries() -> None:
    assert ARTIFACT_TOOLS <= set(REGISTRY)
    assert ARTIFACT_MUTATIONS <= RECOVERY_MUTATIONS
    assert ARTIFACT_MUTATIONS <= REPAIR_MUTATIONS
    assert ARTIFACT_MUTATIONS <= RECEIPT_MUTATIONS
    assert permission_for_tool("artifact.pdf.extract") == "filesystem.read"
    assert permission_for_tool("artifact.validate") == "filesystem.read"
    assert permission_for_tool("artifact.docx.create") == "filesystem.write"
    assert mutation_lock_paths("artifact.pdf.merge", {"path": "merged.pdf"}) == (
        "merged.pdf",
    )
    assert mutation_lock_paths(
        "artifact.render",
        {"path": "report.pdf", "output_directory": "rendered"},
    ) == ("rendered",)
    capabilities = asyncio.run(LocalWindowsExecutor().capabilities())
    assert "artifacts" in capabilities.features


def test_artifact_registry_validates_nested_array_shapes_and_version_tokens() -> None:
    with pytest.raises(ToolValidationError, match="expected_version_token"):
        validate_arguments(
            "artifact.docx.edit",
            {"path": "report.docx", "replacements": [{"old": "a", "new": "b"}]},
        )
    with pytest.raises(ToolValidationError, match="object"):
        validate_arguments(
            "artifact.pptx.create",
            {"path": "deck.pptx", "slides": ["not-an-object"]},
        )
    validate_arguments(
        "artifact.docx.edit",
        {
            "path": "report.docx",
            "replacements": [{"old": "a", "new": "b"}],
            "expected_version_token": "file:1:placeholder",
        },
    )


def test_document_profile_and_daily_report_use_the_artifact_engine() -> None:
    profile = BUILTIN_PROFILES["documents"]
    assert ARTIFACT_TOOLS <= set(profile.tool_allowlist)
    manifest = BUILTIN_SKILLS["daily-report"]["manifest"]
    assert "artifact.docx.create" in manifest.requires_tools
    assert "artifact.validate" in manifest.requires_tools
    assert _required_permissions(manifest.requires_tools) <= {
        "filesystem.read",
        "filesystem.write",
        "artifacts.write",
    }
    assert {"filesystem.write", "artifacts.write"} <= _required_permissions(
        ("artifact.docx.create",),
    )


def test_binary_artifact_writes_are_atomic_versioned_and_recoverable(tmp_path) -> None:
    result = write_workspace_binary(
        str(tmp_path),
        "report.docx",
        b"synthetic-docx",
        operation="artifact.docx.create",
        create_only=True,
        task_id="task-artifact",
        tool_call_id="call-create",
    )
    assert result["success"] is True
    assert result["path"] == "report.docx"
    recovered = recover_file_operation(str(tmp_path), "task-artifact", "call-create")
    assert recovered is not None
    assert recovered["success"] is True
    assert recovered["paths"] == ["report.docx"]

    version = file_version_token(tmp_path / "report.docx")
    edited = write_workspace_binary(
        str(tmp_path),
        "report.docx",
        b"edited-docx",
        operation="artifact.docx.edit",
        expected_version_token=version,
        task_id="task-artifact",
        tool_call_id="call-edit",
    )
    assert edited["version_before"]["sha256"] != edited["version_after"]["sha256"]
    with pytest.raises(FileVersionError) as stale:
        write_workspace_binary(
            str(tmp_path),
            "report.docx",
            b"stale-write",
            operation="artifact.docx.edit",
            expected_version_token=version,
        )
    assert stale.value.code == "version_conflict"


def test_render_outputs_share_one_recovery_record(tmp_path) -> None:
    (tmp_path / "rendered").mkdir()
    result = write_workspace_binaries(
        str(tmp_path),
        {
            "rendered/page-001.png": b"png-one",
            "rendered/page-002.png": b"png-two",
        },
        operation="artifact.render",
        task_id="task-render",
        tool_call_id="call-render",
    )
    assert result["success"] is True
    assert result["paths"] == [
        "rendered/page-001.png",
        "rendered/page-002.png",
    ]
    recovered = recover_file_operation(str(tmp_path), "task-render", "call-render")
    assert recovered is not None
    assert recovered["paths"] == [
        "rendered/page-001.png",
        "rendered/page-002.png",
    ]
    receipt = build_tool_receipt("artifact.render", result)
    assert receipt.changed_files == (
        "rendered/page-001.png",
        "rendered/page-002.png",
    )
