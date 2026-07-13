import uuid
from pathlib import Path

from app.context import context_stats, model_history
from app.database import connect, now_iso


def test_context_stats_and_history_preserve_roles(tmp_path: Path) -> None:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    with connect() as db:
        db.execute("INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)", (conversation_id, "上下文", str(tmp_path), "ask", now_iso(), now_iso()))
        db.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)", (conversation_id, "user", "你好", now_iso()))
        db.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)", (conversation_id, "assistant", "你好，有什么任务？", now_iso()))
    stats = context_stats(conversation_id)
    assert stats["message_count"] == 2
    assert [item["role"] for item in model_history(conversation_id)] == ["user", "assistant"]
