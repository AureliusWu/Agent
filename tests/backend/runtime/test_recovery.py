import asyncio
import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.database import connect, init_db, now_iso
from app.cognition.planning import build_task_plan, save_task_plan
from app.runtime.recovery import create_checkpoint, list_checkpoints, prepare_operation
from app.sandbox import execute_tool
from app.schemas import ChatRequest
from app.runtime.runner import interrupt_running_tasks, run_chat


def _conversation(workspace: Path, *, mode: str = "full") -> int:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "recovery", str(workspace), mode, now_iso(), now_iso()),
        )
    return conversation_id


def test_shutdown_interrupt_creates_checkpoint_and_resume_finishes(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path)
    task_id = uuid.uuid4().hex
    started = asyncio.Event()

    async def slow_completion(messages, api_key=None, **kwargs):
        started.set()
        await asyncio.Event().wait()

    async def final_completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "resumed"}

    async def scenario() -> tuple[dict, dict]:
        running = asyncio.create_task(
            run_chat(
                ChatRequest(conversation_id=conversation_id, content="Respond with resumed.", task_id=task_id),
                completion_fn=slow_completion,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        interrupt_running_tasks()
        interrupted = await asyncio.wait_for(running, timeout=2)
        resumed = await run_chat(
            ChatRequest(conversation_id=conversation_id, content="Respond with resumed.", task_id=task_id, resume=True),
            completion_fn=final_completion,
        )
        return interrupted, resumed

    interrupted, resumed = asyncio.run(scenario())
    assert interrupted["task_status"] == "interrupted"
    assert interrupted["resumable"] is True
    assert resumed["task_status"] == "completed"
    checkpoints = list_checkpoints(task_id)
    assert any(item["reason"] == "application_shutdown" for item in checkpoints)
    with connect() as db:
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())
    assert task["resume_count"] == 1
    assert task["resumable"] == 0


def test_crash_after_file_write_recovers_without_duplicate_side_effect(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path)
    task_id = uuid.uuid4().hex
    prompt = "Create note.txt with the text recovered."
    call = {
        "id": "write-once",
        "type": "function",
        "function": {"name": "create_file", "arguments": json.dumps({"path": "note.txt", "content": "recovered"})},
    }
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, current_step, created_at, updated_at, started_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, conversation_id, "running", prompt, "implementation", stamp, stamp, stamp),
        )
    plan = build_task_plan(task_id, prompt, ["create_file"])
    save_task_plan(plan)
    checkpoint = create_checkpoint(
        task_id,
        str(tmp_path),
        "implementation",
        "before_side_effect",
        {
            "goal": prompt,
            "executor_messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": None, "tool_calls": [call]},
            ],
            "pending_tool_calls": [call],
            "pending_steps": ["tool:create_file"],
            "round_number": 1,
            "model_calls": 1,
        },
    )
    operation = prepare_operation(task_id, checkpoint["sequence"], call, {"path": "note.txt", "content": "recovered"}, side_effect=True)
    write_result = execute_tool(
        str(tmp_path),
        "full",
        "create_file",
        {"path": "note.txt", "content": "recovered"},
        conversation_id=conversation_id,
        task_id=task_id,
        tool_call_id="write-once",
    )
    assert write_result["success"] is True
    with connect() as db:
        db.execute("UPDATE agent_tasks SET status='interrupted', resumable=1 WHERE id=?", (task_id,))

    async def final_completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "The file was recovered without repeating the write."}

    with pytest.raises(HTTPException) as drift:
        asyncio.run(
            run_chat(
                ChatRequest(conversation_id=conversation_id, content=prompt, task_id=task_id, resume=True),
                completion_fn=final_completion,
            )
        )
    assert drift.value.status_code == 409
    assert drift.value.detail["code"] == "workspace_drift"

    resumed = asyncio.run(
        run_chat(
            ChatRequest(
                conversation_id=conversation_id,
                content=prompt,
                task_id=task_id,
                resume=True,
                allow_workspace_drift=True,
            ),
            completion_fn=final_completion,
        )
    )
    assert resumed["task_status"] == "completed"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "recovered"
    backup_folders = [item for item in (tmp_path / ".agent-backups").iterdir() if item.is_dir()]
    assert len(backup_folders) == 1
    with connect() as db:
        recorded_operation = dict(db.execute("SELECT * FROM task_operations WHERE execution_id=?", (operation["execution_id"],)).fetchone())
        tool_run = dict(db.execute(
            "SELECT lease_generation,output FROM tool_runs WHERE task_id=? AND tool='create_file'",
            (task_id,),
        ).fetchone())
    assert recorded_operation["status"] == "completed"
    receipt = json.loads(recorded_operation["result"])
    assert receipt["metadata"]["lease_generation"] == recorded_operation["lease_generation"]
    assert tool_run["lease_generation"] == recorded_operation["lease_generation"]
    assert json.loads(tool_run["output"])["metadata"]["lease_generation"] == recorded_operation["lease_generation"]
