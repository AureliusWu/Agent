import uuid
from pathlib import Path

from app.config import settings
from app.long_term_memory import create_memory
from app.memory_consolidator import consolidate_memories, latest_continuity


def test_continuity_snapshot_is_source_backed_and_written_outside_source_tree() -> None:
    marker = uuid.uuid4().hex
    memory = create_memory(
        memory_type="episodic",
        title="测试共同事件",
        content=f"管理员与夏目心完成了 {marker} 验证",
        source_type="user_confirmed",
        user_confirmed=True,
        importance=1,
    )
    result = consolidate_memories(trigger_type="manual")
    continuity = latest_continuity()
    path = Path(continuity["path"])
    assert result["continuity_snapshot_id"] == continuity["id"]
    assert memory["id"] in continuity["memory_ids"]
    assert marker in continuity["summary"]
    assert path.parent == Path(settings.database_path).resolve().parent
    assert marker in path.read_text(encoding="utf-8")


def test_consolidator_does_not_archive_locked_or_confirmed_memory() -> None:
    memory = create_memory(
        memory_type="semantic",
        content=f"受保护事实 {uuid.uuid4().hex}",
        source_type="user_confirmed",
        user_confirmed=True,
        is_locked=True,
        importance=0.1,
    )
    result = consolidate_memories(trigger_type="manual")
    assert memory["id"] not in result["archived_memory_ids"]
