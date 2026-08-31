from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database as database_module
from app.database import connect, init_db, now_iso
from app.runtime.recovery import create_checkpoint
from app.runtime.runner import run_chat
from app.runtime.task_runtime import _create_pending_task
from app.schemas import ChatRequest


def _conversation(workspace: Path | None = None, *, permission_mode: str = "ask") -> int:
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("budget-v2", str(workspace or ""), permission_mode, stamp, stamp),
        )
    return int(cursor.lastrowid)


def _drop_v43_task_columns(db: sqlite3.Connection) -> None:
    columns = {
        "segment_timeout_seconds",
        "task_deadline_at",
        "token_budget_limit",
        "token_budget_mode",
        "cost_budget_limit",
    }
    existing = {str(row[1]) for row in db.execute("PRAGMA table_info(agent_tasks)")}
    for column in columns & existing:
        db.execute(f"ALTER TABLE agent_tasks DROP COLUMN {column}")


def test_schema42_budget_migration_is_incremental_and_preserves_representative_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "schema42.db"
    stamp = "2026-08-01T00:00:00+00:00"
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript(database_module.SCHEMA)
        _drop_v43_task_columns(db)
        db.executemany(
            "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
            [(version, stamp) for version in range(1, 43)],
        )
        conversation_id = int(
            db.execute(
                "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("readonly-state", str(tmp_path), "readonly", stamp, stamp),
            ).lastrowid
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,total_tokens,estimated_cost_usd,"
            "provider_profile_snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "schema42-task",
                conversation_id,
                "partially_completed",
                "保留这个任务",
                321,
                0.125,
                json.dumps({"id": "deepseek", "default_model": "deepseek-chat"}),
                stamp,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO workspace_memories(workspace,key,content,created_at,updated_at) VALUES(?,?,?,?,?)",
            (str(tmp_path), "decision", "保留这条记忆", stamp, stamp),
        )
        provider_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(provider_capabilities)")}
        provider_values = {
            "provider": "deepseek",
            "endpoint_hash": "representative-endpoint",
            "model": "deepseek-chat",
            "status": "available",
            "capabilities": json.dumps({"tool_calling": True}),
            "sample_count": 1,
            "success_count": 1,
            "observed_at": stamp,
            "expires_at": 4_102_444_800.0,
        }
        selected = [name for name in provider_values if name in provider_columns]
        if selected:
            db.execute(
                f"INSERT INTO provider_capabilities({','.join(selected)}) VALUES({','.join('?' for _ in selected)})",
                tuple(provider_values[name] for name in selected),
            )

    monkeypatch.setattr(database_module.settings, "database_path", database)
    init_db()

    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(agent_tasks)")}
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id='schema42-task'").fetchone())
        conversation = dict(db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone())
        memory = dict(db.execute("SELECT * FROM workspace_memories WHERE key='decision'").fetchone())
        provider = dict(
            db.execute(
                "SELECT * FROM provider_capabilities WHERE endpoint_hash='representative-endpoint'"
            ).fetchone()
        )
        version = int(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])

    assert database_module.SCHEMA_VERSION >= 43
    assert version == database_module.SCHEMA_VERSION
    assert {
        "segment_timeout_seconds",
        "task_deadline_at",
        "token_budget_limit",
        "token_budget_mode",
        "cost_budget_limit",
    } <= columns
    assert task["prompt"] == "保留这个任务"
    assert task["total_tokens"] == 321
    assert task["estimated_cost_usd"] == 0.125
    assert json.loads(task["provider_profile_snapshot"])["id"] == "deepseek"
    assert task["token_budget_mode"] == "soft"
    assert conversation["permission_mode"] == "readonly"
    assert memory["content"] == "保留这条记忆"
    assert provider["provider"] == "deepseek"
    assert json.loads(provider["capabilities"])["tool_calling"] is True
    assert len(list((tmp_path / "backups").glob(f"pre-migration-v42-to-v{database_module.SCHEMA_VERSION}-*.db"))) == 1


def test_hard_token_and_cost_contract_is_persisted_when_task_is_created(tmp_path: Path) -> None:
    async def one_answer(messages, api_key=None, **kwargs):
        return {
            "role": "assistant",
            "content": "完成",
            "_metrics": {
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                "estimated_cost_usd": 0.002,
            },
        }

    conversation_id = _conversation()
    task_id = uuid.uuid4().hex
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    result = asyncio.run(
        run_chat(
            ChatRequest(
                conversation_id=conversation_id,
                content="只分析这个目录",
                task_id=task_id,
                token_budget_limit=5_000,
                token_budget_mode="hard",
                cost_budget_limit=0.25,
                task_deadline_at=deadline,
                segment_timeout_seconds=12,
            ),
            completion_fn=one_answer,
        )
    )

    with connect() as db:
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    assert result["task_status"] == "completed"
    assert task["token_budget_limit"] == 5_000
    assert task["token_budget_mode"] == "hard"
    assert task["cost_budget_limit"] == 0.25
    assert task["segment_timeout_seconds"] == 12
    assert datetime.fromisoformat(task["task_deadline_at"]) == deadline
    assert task["total_tokens"] >= 30
    assert task["estimated_cost_usd"] >= 0.002


def test_background_queue_creation_persists_limits_before_worker_claim(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path)
    task_id = uuid.uuid4().hex
    deadline = datetime.now(timezone.utc) + timedelta(minutes=10)
    payload = ChatRequest(
        conversation_id=conversation_id,
        content="排队任务预算",
        task_id=task_id,
        token_budget_limit=2_000,
        token_budget_mode="hard",
        cost_budget_limit=0.5,
        task_deadline_at=deadline,
        segment_timeout_seconds=7,
    )

    _create_pending_task(payload, None, None)

    with connect() as db:
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())
    assert task["status"] == "pending"
    assert task["token_budget_limit"] == 2_000
    assert task["token_budget_mode"] == "hard"
    assert task["cost_budget_limit"] == 0.5
    assert task["segment_timeout_seconds"] == 7
    assert datetime.fromisoformat(task["task_deadline_at"]) == deadline


def test_cost_budget_is_a_separate_hard_stop_from_token_budget(tmp_path: Path) -> None:
    calls = 0

    async def costly_answer(messages, api_key=None, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "role": "assistant",
            "content": "不应被当作完成",
            "_metrics": {
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "estimated_cost_usd": 0.02,
            },
        }

    conversation_id = _conversation(tmp_path)
    task_id = uuid.uuid4().hex
    result = asyncio.run(
        run_chat(
            ChatRequest(
                conversation_id=conversation_id,
                content="测试成本预算",
                task_id=task_id,
                token_budget_limit=50_000,
                token_budget_mode="hard",
                cost_budget_limit=0.01,
            ),
            completion_fn=costly_answer,
        )
    )

    with connect() as db:
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    assert calls == 1
    assert result["task_status"] == "partially_completed"
    assert task["current_step"] == "cost_budget_limit"
    assert "成本预算" in str(task["termination_reason"])
    assert task["estimated_cost_usd"] >= 0.02


def test_task_deadline_interrupts_a_model_wait_before_segment_timeout(tmp_path: Path) -> None:
    async def slow_answer(messages, api_key=None, **kwargs):
        await asyncio.sleep(1)
        return {"role": "assistant", "content": "too late"}

    conversation_id = _conversation(tmp_path)
    task_id = uuid.uuid4().hex
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=150)
    result = asyncio.run(
        run_chat(
            ChatRequest(
                conversation_id=conversation_id,
                content="测试绝对截止时间",
                task_id=task_id,
                task_deadline_at=deadline,
                segment_timeout_seconds=10,
            ),
            completion_fn=slow_answer,
        )
    )

    with connect() as db:
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    assert result["task_status"] == "timed_out"
    assert task["current_step"] == "task_deadline"
    assert task["resumable"] == 0
    assert "截止时间" in str(task["termination_reason"])


def test_resume_restores_persisted_hard_token_contract_instead_of_request_defaults(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path)
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,total_tokens,input_tokens,"
            "provider_profile_snapshot,token_budget_limit,token_budget_mode,segment_timeout_seconds,"
            "current_phase,current_step,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                conversation_id,
                "interrupted",
                "恢复预算",
                90,
                90,
                "{}",
                100,
                "hard",
                30,
                "analysis",
                "interrupted",
                stamp,
                stamp,
            ),
        )
    create_checkpoint(
        task_id,
        str(tmp_path),
        "analysis",
        "test_resume",
        {
            "goal": "恢复预算",
            "total_tokens": 90,
            "input_tokens": 90,
            "context_summary": "resume budget contract",
        },
    )
    calls = 0

    async def should_not_run(messages, api_key=None, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "role": "assistant",
            "content": '{"steps":[],"acceptance_criteria":[],"risk":"low"}',
            "_metrics": {"usage": {"prompt_tokens": 15, "completion_tokens": 10, "total_tokens": 25}},
        }

    result = asyncio.run(
        run_chat(
            ChatRequest(
                conversation_id=conversation_id,
                content="恢复预算",
                task_id=task_id,
                resume=True,
            ),
            completion_fn=should_not_run,
        )
    )

    with connect() as db:
        task = dict(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    assert calls == 0
    assert result["task_status"] == "partially_completed"
    assert task["token_budget_limit"] == 100
    assert task["token_budget_mode"] == "hard"
    assert task["total_tokens"] == 90
    assert "Token 预算" in str(task["termination_reason"])
