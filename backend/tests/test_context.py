import asyncio
import json
import uuid
from pathlib import Path

from app.context import compact_conversation, context_stats, load_working_memory, model_history, save_working_memory
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


def test_structured_compaction_preserves_constraints_and_next_action(tmp_path: Path, monkeypatch) -> None:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    with connect() as db:
        db.execute("INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)", (conversation_id, "压缩", str(tmp_path), "ask", now_iso(), now_iso()))
        for index in range(12):
            role = "user" if index % 2 == 0 else "assistant"
            db.execute("INSERT INTO messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)", (conversation_id, role, f"消息 {index}", now_iso()))

    async def fake_completion(*_args, **_kwargs):
        return {"content": json.dumps({
            "goal": "只修改 context.py 并完成验证",
            "constraints": ["不要修改其他文件", "必须运行测试"],
            "plan": ["实现结构化压缩"],
            "completed": ["完成审计"],
            "pending": ["运行测试"],
            "modified_files": ["backend/app/context.py"],
            "current_errors": [],
            "decisions": ["摘要使用固定字段"],
            "verification": ["尚未运行"],
            "next_actions": ["运行 pytest"],
        }, ensure_ascii=False)}

    monkeypatch.setattr("app.context.completion", fake_completion)
    result = asyncio.run(compact_conversation(conversation_id, "test", force=True))
    history = model_history(conversation_id)

    assert result["compacted"] is True
    assert result["structured"] is True
    assert result["structured_summary"]["constraints"] == ["不要修改其他文件", "必须运行测试"]
    assert "不要修改其他文件" in history[0]["content"]
    assert "运行 pytest" in history[0]["content"]


def test_working_memory_round_trip(tmp_path: Path) -> None:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    with connect() as db:
        db.execute("INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)", (conversation_id, "工作记忆", str(tmp_path), "ask", now_iso(), now_iso()))
        db.execute("INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)", (task_id, conversation_id, "running", "继续任务", now_iso(), now_iso()))
    save_working_memory(task_id, {"constraints": ["不得越过工作区"], "pending_steps": ["verify"]})
    assert load_working_memory(task_id)["pending_steps"] == ["verify"]
