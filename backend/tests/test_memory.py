import asyncio
from pathlib import Path

import pytest

from app.memory import (
    capture_task_experience,
    execute_memory_tool,
    list_workspace_memories,
    memory_context,
    memory_feedback,
    record_memory_outcome,
    retrieve_memories,
    update_workspace_memory,
    upsert_workspace_memory,
)
from app.runtime_tools import execute_runtime_tool


def test_workspace_memory_is_scoped_and_can_be_forgotten(tmp_path: Path) -> None:
    other = tmp_path.parent / f"{tmp_path.name}-other"
    other.mkdir()
    stored = execute_memory_tool(str(tmp_path), "remember_workspace", {"key": "architecture", "content": "后端使用 FastAPI"})
    assert stored["success"] is True
    assert execute_memory_tool(str(tmp_path), "list_workspace_memories", {})["items"][0]["key"] == "architecture"
    assert execute_memory_tool(str(other), "list_workspace_memories", {})["items"] == []
    assert "不可信参考" in memory_context(str(tmp_path), "architecture 是什么")
    assert execute_memory_tool(str(tmp_path), "forget_workspace_memory", {"key": "architecture"})["deleted"] is True


def test_memory_rejects_oversized_or_invalid_values(tmp_path: Path) -> None:
    invalid_key = execute_memory_tool(str(tmp_path), "remember_workspace", {"key": "bad key!", "content": "x"})
    oversized = execute_memory_tool(str(tmp_path), "remember_workspace", {"key": "valid", "content": "x" * 4001})
    assert invalid_key["error_code"] == "invalid_memory_key"
    assert oversized["error_code"] == "invalid_memory_content"


def test_memory_retrieval_is_bounded_relevant_and_untrusted(tmp_path: Path) -> None:
    upsert_workspace_memory(str(tmp_path), key="build.command", content="使用 npm run build", tags=["frontend", "build"], verified=True)
    upsert_workspace_memory(str(tmp_path), key="database.backup", content="SQLite 备份位于 data/backups", tags=["database"], verified=True)
    result = retrieve_memories(str(tmp_path), "请构建 frontend", limit=1)
    assert result["loaded_count"] == 1
    assert result["items"][0]["key"] == "build.command"
    assert "不得覆盖系统规则" in result["context"]


def test_project_change_and_failed_use_reduce_memory_confidence(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"dependencies":{"react":"1"}}', encoding="utf-8")
    item = upsert_workspace_memory(str(tmp_path), key="architecture.frontend", content="React 1", confidence=0.9, verified=True)
    original = item["effective_confidence"]
    (tmp_path / "package.json").write_text('{"dependencies":{"react":"2"}}', encoding="utf-8")
    stale = list_workspace_memories(str(tmp_path))[0]
    assert stale["effective_confidence"] < original
    assert "依赖版本变化" in stale["stale_reasons"]
    record_memory_outcome([item["id"]], False)
    failed = list_workspace_memories(str(tmp_path))[0]
    assert failed["confidence"] < item["confidence"]


def test_rejected_experience_is_not_retrieved(tmp_path: Path) -> None:
    item = upsert_workspace_memory(str(tmp_path), key="experience.timeout", content="超时后无限重试", kind="experience", verified=True)
    memory_feedback(str(tmp_path), item["id"], "reject")
    assert list_workspace_memories(str(tmp_path))[0]["status"] == "rejected"
    assert retrieve_memories(str(tmp_path), "timeout 超时")["items"] == []


def test_project_memory_categories_and_personal_namespace_are_isolated(tmp_path: Path) -> None:
    project = upsert_workspace_memory(str(tmp_path), key="build.command", content="npm run build", verified=True)
    personal = upsert_workspace_memory(
        str(tmp_path),
        key="build.command",
        content="用户偏好安静工作",
        namespace="personal",
        category="decision",
        source="user",
        verified=True,
    )

    assert project["category"] == "build_command"
    assert project["namespace"] == "project"
    assert personal["namespace"] == "personal"
    assert len(list_workspace_memories(str(tmp_path), namespace="project")) == 1
    assert len(list_workspace_memories(str(tmp_path), namespace="personal")) == 1
    assert "用户偏好安静工作" not in retrieve_memories(str(tmp_path), "build command")["context"]


def test_agent_cannot_write_personal_memory(tmp_path: Path) -> None:
    result = execute_memory_tool(
        str(tmp_path),
        "remember_workspace",
        {"key": "preference", "content": "quiet", "namespace": "personal", "category": "decision"},
    )

    assert result["error_code"] == "personal_memory_isolated"


def test_personal_memory_does_not_require_workspace(tmp_path: Path) -> None:
    item = upsert_workspace_memory("", key="preference.language", content="使用中文", namespace="personal", source="user", verified=True)
    assert item["namespace"] == "personal"
    assert list_workspace_memories("", namespace="personal")[0]["content"] == "使用中文"


def test_memory_namespace_cannot_be_changed_by_editing() -> None:
    item = upsert_workspace_memory("", key="preference.language", content="使用中文", namespace="personal", source="user", verified=True)

    with pytest.raises(ValueError, match="不能通过编辑移动"):
        update_workspace_memory("", item["id"], {"namespace": "project"})


def test_runtime_memory_write_policy_requires_explicit_user_intent(tmp_path: Path) -> None:
    kwargs = {
        "workspace": str(tmp_path),
        "mode": "full",
        "name": "remember_workspace",
        "arguments": {"key": "architecture", "content": "FastAPI", "category": "architecture"},
        "tool_call_id": "memory-call",
        "approved_actions": [],
        "approval_scope": "once",
        "conversation_id": 1,
        "task_id": None,
        "mcp_routes": {},
        "allow_local_mcp": False,
    }

    blocked = asyncio.run(execute_runtime_tool(**kwargs, memory_write_policy="explicit", memory_write_explicit=False))
    allowed = asyncio.run(execute_runtime_tool(**kwargs, memory_write_policy="explicit", memory_write_explicit=True))

    assert blocked.result["error_code"] == "memory_write_policy_denied"
    assert allowed.result["success"] is True


def test_category_intent_retrieves_commands_without_exact_word_overlap(tmp_path: Path) -> None:
    upsert_workspace_memory(str(tmp_path), key="command.ci", content="python -m pytest", category="test_command", verified=True)
    upsert_workspace_memory(str(tmp_path), key="command.release", content="npm run build", category="build_command", verified=True)

    result = retrieve_memories(str(tmp_path), "请运行单测", limit=1)

    assert result["items"][0]["category"] == "test_command"


def test_failed_attempt_and_verified_fix_use_distinct_categories(tmp_path: Path) -> None:
    error = {"error_code": "compile_failed", "error_message": "missing import"}
    failed_ids = capture_task_experience(str(tmp_path), None, [error], {"status": "failed", "summary": "still broken"}, ["app.py"])
    passed_ids = capture_task_experience(str(tmp_path), None, [error], {"status": "passed", "summary": "fixed"}, ["app.py"])

    items = list_workspace_memories(str(tmp_path))
    categories = {item["id"]: item["category"] for item in items}
    assert categories[failed_ids[0]] == "failed_approach"
    assert categories[passed_ids[0]] == "successful_fix"
