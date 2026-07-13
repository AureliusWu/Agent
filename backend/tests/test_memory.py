from pathlib import Path

from app.memory import execute_memory_tool, memory_context


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
