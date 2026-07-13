import asyncio
import uuid
from pathlib import Path

from app.database import connect, now_iso
from app.schemas import ChatRequest
from app.task_runner import cancel_task, run_chat


def test_running_model_request_can_be_interrupted(tmp_path: Path, monkeypatch) -> None:
    started = asyncio.Event()

    async def slow_completion(messages, api_key=None, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.task_runner.completion", slow_completion)
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "取消测试", str(tmp_path), "full", now_iso(), now_iso()),
        )

    async def scenario() -> tuple[dict, dict]:
        running = asyncio.create_task(run_chat(ChatRequest(conversation_id=conversation_id, content="等待", task_id=task_id)))
        await asyncio.wait_for(started.wait(), timeout=2)
        cancellation = cancel_task(task_id)
        result = await asyncio.wait_for(running, timeout=2)
        return cancellation, result

    cancellation, result = asyncio.run(scenario())
    assert cancellation["interrupted"] is True
    assert result["task_status"] == "cancelled"
    with connect() as db:
        status = db.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0]
    assert status == "cancelled"
