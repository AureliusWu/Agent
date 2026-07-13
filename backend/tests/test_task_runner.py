import asyncio
import uuid
from pathlib import Path

from app.database import connect, init_db, now_iso
from app.schemas import ChatRequest
from app.task_runner import cancel_task, run_chat


def test_running_model_request_can_be_interrupted(tmp_path: Path, monkeypatch) -> None:
    init_db()
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


def _conversation(tmp_path: Path) -> int:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "循环限制", str(tmp_path), "full", now_iso(), now_iso()),
        )
    return conversation_id


def test_task_stops_at_token_budget_and_records_final_state(tmp_path: Path, monkeypatch) -> None:
    async def expensive_completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "不应直接完成", "_metrics": {"usage": {"total_tokens": 11}}}

    monkeypatch.setattr("app.task_runner.completion", expensive_completion)
    monkeypatch.setattr("app.task_runner.settings.max_task_tokens", 10)
    conversation_id, task_id = _conversation(tmp_path), uuid.uuid4().hex

    result = asyncio.run(run_chat(ChatRequest(conversation_id=conversation_id, content="测试预算", task_id=task_id)))
    with connect() as db:
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    assert result["task_status"] == "partially_completed"
    assert task["total_tokens"] == 11
    assert task["finished_at"] is not None
    assert task["current_step"] == "token_limit"


def test_task_stops_at_total_tool_call_limit(tmp_path: Path, monkeypatch) -> None:
    async def two_tools(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "one", "type": "function", "function": {"name": "list_files", "arguments": '{"path":"."}'}},
            {"id": "two", "type": "function", "function": {"name": "list_files", "arguments": '{"path":"./"}'}},
        ]}

    monkeypatch.setattr("app.task_runner.completion", two_tools)
    monkeypatch.setattr("app.task_runner.settings.max_tool_calls", 1)
    conversation_id, task_id = _conversation(tmp_path), uuid.uuid4().hex

    result = asyncio.run(run_chat(ChatRequest(conversation_id=conversation_id, content="测试工具上限", task_id=task_id)))

    assert result["task_status"] == "partially_completed"
    assert "工具调用达到上限" in result["content"]


def test_task_detects_rounds_without_progress(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    async def equivalent_reads(messages, api_key=None, **kwargs):
        nonlocal calls
        calls += 1
        path = "." if calls % 2 else "./"
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": str(calls), "type": "function", "function": {"name": "list_files", "arguments": f'{{"path":"{path}"}}'}},
        ]}

    monkeypatch.setattr("app.task_runner.completion", equivalent_reads)
    monkeypatch.setattr("app.task_runner.settings.max_no_progress_rounds", 1)
    monkeypatch.setattr("app.task_runner.settings.max_duplicate_tool_calls", 10)
    conversation_id, task_id = _conversation(tmp_path), uuid.uuid4().hex

    result = asyncio.run(run_chat(ChatRequest(conversation_id=conversation_id, content="测试无进展", task_id=task_id)))

    assert result["task_status"] == "partially_completed"
    assert "没有有效进展" in result["content"]


def test_task_timeout_interrupts_inflight_model_call(tmp_path: Path, monkeypatch) -> None:
    async def never_finishes(messages, api_key=None, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("app.task_runner.completion", never_finishes)
    monkeypatch.setattr("app.task_runner.settings.task_timeout_seconds", 0.2)
    conversation_id, task_id = _conversation(tmp_path), uuid.uuid4().hex

    result = asyncio.run(run_chat(ChatRequest(conversation_id=conversation_id, content="测试总超时", task_id=task_id)))
    with connect() as db:
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    assert result["task_status"] == "timed_out"
    assert task["model_calls"] == 1
    assert task["finished_at"] is not None


def test_code_task_is_only_partial_without_post_change_verification(tmp_path: Path, monkeypatch) -> None:
    call_number = 0

    async def write_then_finish(messages, api_key=None, **kwargs):
        nonlocal call_number
        call_number += 1
        if call_number == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "write", "type": "function",
                "function": {"name": "create_file", "arguments": '{"path":"main.py","content":"print(1)"}'},
            }]}
        return {"role": "assistant", "content": "代码已写入"}

    monkeypatch.setattr("app.task_runner.completion", write_then_finish)
    conversation_id, task_id = _conversation(tmp_path), uuid.uuid4().hex
    with connect() as db:
        db.execute("UPDATE conversations SET permission_mode='agent' WHERE id=?", (conversation_id,))

    result = asyncio.run(run_chat(ChatRequest(conversation_id=conversation_id, content="创建 Python 代码", task_id=task_id)))

    assert result["task_status"] == "partially_completed"
    assert result["verification"]["status"] == "partial"
    assert any(item["status"] == "not_run" for item in result["verification"]["checks"])
