"""MEMORY-LINEAGE-001: scope-bound evidence and revision-aware model context."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from app.context.assembler import assemble_context, context_debug
from app.context.budget import compact_messages_deterministically
from app.database import connect, now_iso, rows
from app.memory.lineage import LINEAGE_KEY, MemorySourceError
from app.memory.service import (
    capture_task_experience, execute_memory_tool, list_workspace_memories,
    memory_feedback, memory_references_current, record_memory_outcome, retrieve_memories,
    update_workspace_memory, upsert_workspace_memory,
)
from app.providers.provider import ProviderError
from app.runtime.runner import run_chat
from app.schemas import ChatRequest


def _conversation(workspace: Path) -> int:
    stamp = now_iso()
    with connect() as db:
        return int(db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("memory lineage fixture", str(workspace), "full", stamp, stamp),
        ).lastrowid)


def _origin(workspace: Path) -> tuple[int, str, int]:
    conversation = _conversation(workspace)
    task = uuid.uuid4().hex
    stamp = now_iso()
    prompt = "original user fixture, never copied into evidence metadata"
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task, conversation, "completed", prompt, stamp, stamp),
        )
        message = db.execute(
            "INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
            (conversation, "user", prompt, task, stamp),
        ).lastrowid
    return conversation, task, int(message)


def _generated(workspace: Path, task: str, **kwargs):
    return upsert_workspace_memory(
        str(workspace), key="architecture.lineage", content="Fixture component uses durable snapshots.",
        source="agent", source_task_id=task, **kwargs,
    )


def test_host_derives_bound_origin_and_stores_only_fingerprint(tmp_path) -> None:
    conversation, task, message = _origin(tmp_path)
    record = _generated(tmp_path, task, source_message_id="untrusted", source_metadata={LINEAGE_KEY: {"version": 99}})
    assert record["lineage_state"] == "source_matched"
    binding = record["source_metadata"][LINEAGE_KEY]
    assert binding["version"] == 1 and binding["task_id"] == task
    assert binding["conversation_id"] == str(conversation) and binding["message_id"] == str(message)
    assert "never copied" not in json.dumps(binding)
    assert len(binding["source_revision"]) == len(binding["content_revision"]) == 64
    reference = retrieve_memories(str(tmp_path), "architecture.lineage")["references"][0]
    assert reference["source_revision"] == binding["source_revision"]
    assert reference["memory_id"] == str(record["id"])
    assert memory_references_current(str(tmp_path), [reference])


@pytest.mark.parametrize("change,state", [
    ("source_text", "source_changed"), ("source_missing", "source_missing"),
    ("task_missing", "source_missing"), ("workspace", "source_outside_scope"),
    ("role", "source_missing"), ("memory_edit", "memory_changed"),
])
def test_invalid_lineage_visible_to_user_but_not_to_agent(tmp_path, change, state) -> None:
    conversation, task, message = _origin(tmp_path)
    record = _generated(tmp_path, task)
    references = retrieve_memories(str(tmp_path), "architecture.lineage")["references"]
    with connect() as db:
        if change == "source_text":
            db.execute("UPDATE messages SET content=content||' ' WHERE id=?", (message,))
        elif change == "source_missing":
            db.execute("DELETE FROM messages WHERE id=?", (message,))
        elif change == "task_missing":
            db.execute("DELETE FROM agent_tasks WHERE id=?", (task,))
        elif change == "workspace":
            foreign = tmp_path / "foreign"
            foreign.mkdir()
            db.execute("UPDATE conversations SET workspace=? WHERE id=?", (str(foreign), conversation))
        elif change == "role":
            db.execute("UPDATE messages SET role='assistant' WHERE id=?", (message,))
    if change == "memory_edit":
        update_workspace_memory(str(tmp_path), record["id"], {"content": record["content"].upper()})
    visible = list_workspace_memories(str(tmp_path))[0]
    assert visible["lineage_state"] == state and visible["status"] == "stale"
    assert not memory_references_current(str(tmp_path), references)
    assert not retrieve_memories(str(tmp_path), "architecture.lineage")["items"]
    assert not execute_memory_tool(str(tmp_path), "list_workspace_memories", {})["items"]
    assert visible["use_count"] == 1
    with pytest.raises(ValueError, match="置信度反馈不能恢复来源"):
        memory_feedback(str(tmp_path), record["id"], "verify")


def test_real_capture_rejects_missing_or_foreign_origins_without_legacy_downgrade(tmp_path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _, task, _ = _origin(foreign)
    with pytest.raises(MemorySourceError):
        _generated(tmp_path, task)
    with pytest.raises(MemorySourceError):
        _generated(tmp_path, "missing-fixture-task")
    result = execute_memory_tool(str(tmp_path), "remember_workspace", {"key": "architecture", "content": "x"}, task)
    assert result["success"] is False and result["error_code"] == "memory_source_unavailable"
    assert capture_task_experience(str(tmp_path), task, [{"reason": "fixture"}], {"status": "passed"}, []) == []
    assert not list_workspace_memories(str(tmp_path))
    ids = capture_task_experience(str(foreign), task, [{"reason": "fixture"}], {"status": "passed"}, [])
    assert len(ids) == 1
    assert list_workspace_memories(str(foreign))[0]["lineage_state"] == "source_matched"


def test_legacy_and_user_authored_states_do_not_invent_source_proof(tmp_path) -> None:
    old = upsert_workspace_memory(str(tmp_path), key="architecture.old", content="Old fixture.", source="automatic")
    user = upsert_workspace_memory(
        str(tmp_path), key="architecture.manual", content="Manual fixture.",
        source_metadata={LINEAGE_KEY: {"version": 1}}, source_message_id="unverified-client-id",
    )
    assert old["lineage_state"] == "legacy_unverified"
    assert user["lineage_state"] == "user_authored" and LINEAGE_KEY not in user["source_metadata"]
    result = retrieve_memories(str(tmp_path), "architecture")
    assert "旧来源未核验" in result["context"]
    assert all("source_revision" not in reference for reference in result["references"])


def test_fresh_capture_does_not_deduplicate_to_ineligible_old_source(tmp_path) -> None:
    _, first, first_message = _origin(tmp_path)
    stale = _generated(tmp_path, first)
    with connect() as db:
        db.execute("DELETE FROM messages WHERE id=?", (first_message,))
    _, new, _ = _origin(tmp_path)
    repaired = _generated(tmp_path, new)
    assert repaired["id"] == stale["id"]
    assert repaired["lineage_state"] == "source_matched" and repaired["source_task_id"] == new


def test_deterministic_compaction_never_copies_memory_list_into_system_excerpt() -> None:
    messages = [
        {"role": "system", "content": "Keep authority"},
        {"role": "user", "content": "Earlier user request"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "memory-list"}]},
        {"role": "tool", "tool_call_id": "memory-list", "content": "Derived fact later revoked"},
        {"role": "user", "content": "Current instruction must remain"},
    ]
    result, _ = compact_messages_deterministically(
        messages, None, target_input_tokens=1000, memory_list_call_ids={"memory-list"},
    )
    encoded = json.dumps(result, ensure_ascii=False)
    assert "Derived fact later revoked" not in encoded
    assert "已省略历史记忆列表" in encoded
    assert "Current instruction must remain" in encoded


def test_assembly_records_revision_at_selection_not_current_mutable_row(tmp_path) -> None:
    conversation, task, _ = _origin(tmp_path)
    item = _generated(tmp_path, task)
    retrieval = retrieve_memories(str(tmp_path), "architecture.lineage")
    references = retrieval["references"]
    record_memory_outcome([item["id"]], True)
    assert memory_references_current(str(tmp_path), references)
    assembly = assemble_context(
        profile_context="general", task_context="fixture", conversation_id=conversation,
        task_id=task, model=None, project_memory_context=retrieval["context"],
        project_memory_ids=(item["id"],), project_memory_references=tuple(references),
    )
    assert assembly.memory_references == tuple(references)
    update_workspace_memory(str(tmp_path), item["id"], {"content": "New and unverified fixture."})
    assert context_debug(conversation)["memory_references"] == references
    assert not memory_references_current(str(tmp_path), references)
    assert not retrieve_memories(str(tmp_path), "architecture.lineage", max_chars=2)["references"]


@pytest.mark.parametrize("resume", [False, True])
def test_runner_drops_revoked_context_on_later_model_round_or_resume(tmp_path, monkeypatch, resume) -> None:
    monkeypatch.setattr("app.runtime.runner.provider_ready", lambda *_: False)
    _, origin_task, message = _origin(tmp_path)
    item = _generated(tmp_path, origin_task)
    (tmp_path / "fixture.txt").write_text("safe fixture", encoding="utf-8")
    conversation = _conversation(tmp_path)
    task_id = uuid.uuid4().hex
    request = ChatRequest(conversation_id=conversation, task_id=task_id, content="Explain architecture.lineage.",
                          orchestration_mode="single", memory_write_policy="deny")
    model_rounds = []

    async def complete(messages, api_key=None, **kwargs):
        assert api_key is None
        system = messages[0]["content"]
        model_rounds.append(system)
        if len(model_rounds) == 1:
            assert item["content"] in system
            if resume:
                raise ProviderError("isolated interruption", "missing_api_key")
            with connect() as db:
                db.execute("DELETE FROM messages WHERE id=?", (message,))
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "inspect-fixture", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path": "fixture.txt"}),
                },
            }]}
        assert item["content"] not in system
        return {"role": "assistant", "content": "Fixture response."}

    result = asyncio.run(run_chat(request, completion_fn=complete))
    if resume:
        assert result["task_status"] == "waiting_provider_credential"
        checkpoint = rows("SELECT id,state FROM task_checkpoints WHERE task_id=? ORDER BY sequence DESC LIMIT 1", (task_id,))[0]
        stale = json.loads(checkpoint["state"])
        stale["retrieved_memory_ids"] = [item["id"]]
        stale["retrieved_memory_context"] = item["content"]
        with connect() as db:
            db.execute("UPDATE task_checkpoints SET state=? WHERE id=?", (json.dumps(stale), checkpoint["id"]))
            db.execute("DELETE FROM messages WHERE id=?", (message,))
        result = asyncio.run(run_chat(request.model_copy(update={"resume": True}), completion_fn=complete))
    assert result["task_status"] == "completed"
    assert len(model_rounds) == 2
    assert context_debug(conversation)["memory_references"] == []


@pytest.mark.parametrize("resume", [False, True])
def test_runner_never_replays_revoked_memory_list_payload(tmp_path, monkeypatch, resume) -> None:
    monkeypatch.setattr("app.runtime.runner.provider_ready", lambda *_: False)
    _, source_task, source_message = _origin(tmp_path)
    record = _generated(tmp_path, source_task)
    (tmp_path / "fixture.txt").write_text("read only fixture", encoding="utf-8")
    conversation = _conversation(tmp_path)
    task_id = uuid.uuid4().hex
    request = ChatRequest(
        conversation_id=conversation, task_id=task_id, orchestration_mode="single",
        content="Explain list_workspace_memories and read_file fixture.txt.", memory_write_policy="deny",
    )
    rounds = 0

    async def complete(messages, api_key=None, **kwargs):
        nonlocal rounds
        rounds += 1
        assert api_key is None
        if rounds == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "fixture-memory-list", "type": "function", "function": {"name": "list_workspace_memories", "arguments": "{}"},
            }]}
        tool_response = next(item for item in messages if item.get("tool_call_id") == "fixture-memory-list")
        if rounds == 2:
            assert record["content"] in tool_response["content"]
            if resume:
                raise ProviderError("isolated interruption", "missing_api_key")
            with connect() as db:
                db.execute("DELETE FROM messages WHERE id=?", (source_message,))
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "fixture-read", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path": "fixture.txt"}),
                },
            }]}
        assert "stale_memory_list" in tool_response["content"]
        assert record["content"] not in json.dumps(messages, ensure_ascii=False)
        return {"role": "assistant", "content": "Fixture response."}

    result = asyncio.run(run_chat(request, completion_fn=complete))
    if resume:
        assert result["task_status"] == "waiting_provider_credential"
        with connect() as db:
            db.execute("DELETE FROM messages WHERE id=?", (source_message,))
        result = asyncio.run(run_chat(request.model_copy(update={"resume": True}), completion_fn=complete))
    assert result["task_status"] == "completed" and rounds == 3
    raw = rows("SELECT output FROM tool_runs WHERE task_id=? AND tool='list_workspace_memories'", (task_id,))
    assert len(raw) == 1
    # Durable audit output remains the original *redacted* host result, not
    # the model-only stale placeholder. Do not require plaintext in the audit.
    original = json.loads(raw[0]["output"])
    assert original["status"] == "ok" and original["data"]["items"][0]["id"] == record["id"]
    assert original["receipt"]["tool_call_id"] == "fixture-memory-list"
