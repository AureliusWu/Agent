import asyncio
import json
import uuid

import pytest

from app.context.assembler import assemble_context, context_debug
from app.database import connect, now_iso, rows
from app.memory.long_term import create_memory, get_memory
from app.memory.service import upsert_workspace_memory
from app.runtime.runner import run_chat
from app.schemas import ChatRequest


def test_context_assembler_preserves_layers_and_records_project_memory_references() -> None:
    marker = uuid.uuid4().hex
    memory = create_memory(
        memory_type="procedural",
        content=f"处理 {marker} 前必须先检查现有实现",
        source_type="user_confirmed",
        user_confirmed=True,
        importance=1,
        confidence=1,
    )
    assembled = assemble_context(
        profile_context="coding profile",
        task_context="current task",
        conversation_id=99101,
        task_id="task-context-test",
        model="deepseek-v4-pro",
        token_budget=6000,
        project_memory_context="Scoped project fixture",
        project_memory_ids=(42,),
    )
    debug = context_debug(99101)
    assert "Identity Kernel" in assembled.text
    assert "Affect and relationship" in assembled.text
    assert assembled.memory_ids == ("42",)
    assert assembled.text.count("Scoped project fixture") == 1
    assert memory["content"] not in assembled.text
    assert debug["available"] is True
    assert debug["memory_ids"] == ["42"]
    assert debug["layers"]["memory_scope"] == "project"
    assert debug["layers"]["project_memory"] > 0
    assert debug["layers"]["long_term_memory"] == 0
    assert "text" not in debug


def test_legacy_personal_references_are_not_exposed_by_context_diagnostics() -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO context_assemblies(id,agent_id,conversation_id,token_budget,estimated_tokens,"
            "layer_summary_json,memory_ids_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("legacy-personal-synthetic", "natsume-kokoro-001", 99106, 6000, 100,
             '{"long_term_memory":50}', '["mem_legacy_fixture"]', now_iso()),
        )
    debug = context_debug(99106)
    assert debug["available"] is True
    assert debug["memory_ids"] == []
    assert rows("SELECT memory_ids_json FROM context_assemblies WHERE id=?", ("legacy-personal-synthetic",))[0][
        "memory_ids_json"
    ] == '["mem_legacy_fixture"]'


def test_sensitive_memory_is_not_injected_into_context() -> None:
    marker = uuid.uuid4().hex
    sensitive = create_memory(
        memory_type="semantic",
        content=f"{marker} API Key 是 sk-test_DO_NOT_USE_000000000000",
        source_type="manual_entry",
    )
    assembled = assemble_context(
        profile_context="general",
        task_context="answer safely",
        conversation_id=99102,
        task_id=None,
        model="deepseek-v4-flash",
    )
    assert sensitive["id"] not in assembled.memory_ids
    assert "sk-test_DO_NOT_USE_000000000000" not in assembled.text


def test_context_includes_authoritative_runtime_date() -> None:
    assembled = assemble_context(
        profile_context="general",
        task_context="answer from runtime facts",
        conversation_id=99103,
        task_id="date-test",
        model="deepseek-v4-flash",
    )
    assert "[Runtime facts]" in assembled.text
    assert "Current local time:" in assembled.text
    assert "never guess a date" in assembled.text
    assert assembled.layers["runtime"] > 0


def test_fixed_context_prefix_is_byte_stable_across_dynamic_task_changes() -> None:
    first = assemble_context(
        profile_context="stable profile",
        task_context="first task",
        conversation_id=99104,
        task_id="prefix-first",
        model="deepseek-v4-pro",
    )
    second = assemble_context(
        profile_context="stable profile",
        task_context="second task",
        conversation_id=99105,
        task_id="prefix-second",
        model="deepseek-v4-pro",
    )
    assert first.stable_prefix_fingerprint == second.stable_prefix_fingerprint
    assert first.stable_prefix_tokens == second.stable_prefix_tokens
    assert first.text.startswith("[Context boundary]\n")
    assert second.text.startswith("[Context boundary]\n")


@pytest.mark.parametrize("with_workspace", [False, True])
def test_model_boundary_excludes_personal_and_other_workspace_memory(tmp_path, monkeypatch, with_workspace) -> None:
    """SIYI-1700-MEMORY-SCOPE: isolate actual runner requests, not only the retrieval API."""
    monkeypatch.setattr("app.runtime.runner.provider_ready", lambda *_: False)
    own, other = tmp_path / "own", tmp_path / "other"
    own.mkdir()
    other.mkdir()
    marker = uuid.uuid4().hex
    private_text = f"{marker} private personal fixture"
    unrelated = f"{marker} unrelated workspace fixture"
    permitted = f"{marker} permitted workspace fixture"
    personal = create_memory(memory_type="semantic", content=private_text, is_sensitive=False, is_locked=True)
    own_memory = upsert_workspace_memory(str(own), key=marker, content=permitted)
    upsert_workspace_memory(str(other), key=marker, content=unrelated)
    stamp = now_iso()
    with connect() as db:
        conversation_id = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("isolated memory fixture", str(own) if with_workspace else "", "readonly", stamp, stamp),
        ).lastrowid
    before = get_memory(personal["id"])["access_count"]
    requests = []

    async def offline_completion(messages, api_key=None, **_kwargs):
        assert api_key is None
        requests.append(json.dumps(messages, ensure_ascii=False))
        return {"role": "assistant", "content": "isolated mock response"}

    request = ChatRequest(conversation_id=conversation_id, task_id=uuid.uuid4().hex,
                          content=f"解释 {marker}", orchestration_mode="single", memory_write_policy="deny")
    result = asyncio.run(run_chat(request, completion_fn=offline_completion))
    assert result["task_status"] == "completed"
    assert requests
    assert all(private_text not in item and unrelated not in item for item in requests)
    assert any(permitted in item for item in requests) is with_workspace
    assert all(item.count(permitted) <= 1 for item in requests)
    assert get_memory(personal["id"])["content"] == private_text
    assert get_memory(personal["id"])["access_count"] == before
    assert rows("SELECT count(*) AS total FROM model_runs WHERE task_id=?", (request.task_id,))[0]["total"] == 0
    debug = context_debug(conversation_id)
    assert debug["memory_ids"] == ([str(own_memory["id"])] if with_workspace else [])
