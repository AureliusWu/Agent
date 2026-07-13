from pathlib import Path

from app.memory import (
    execute_memory_tool,
    list_workspace_memories,
    memory_context,
    memory_feedback,
    record_memory_outcome,
    retrieve_memories,
    upsert_workspace_memory,
)


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
