import uuid

from app.context_assembler import assemble_context, context_debug
from app.long_term_memory import create_memory


def test_context_assembler_preserves_layers_and_records_memory_references() -> None:
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
        query=marker,
        profile_context="coding profile",
        task_context="current task",
        conversation_id=99101,
        task_id="task-context-test",
        model="deepseek-v4-pro",
        token_budget=6000,
    )
    debug = context_debug(99101)
    assert "Identity Kernel" in assembled.text
    assert "Affect and relationship" in assembled.text
    assert memory["id"] in assembled.memory_ids
    assert memory["content"] in assembled.text
    assert debug["available"] is True
    assert memory["id"] in debug["memory_ids"]
    assert "text" not in debug


def test_sensitive_memory_is_not_injected_into_context() -> None:
    marker = uuid.uuid4().hex
    sensitive = create_memory(
        memory_type="semantic",
        content=f"{marker} API Key 是 sk-test_DO_NOT_USE_000000000000",
        source_type="manual_entry",
    )
    assembled = assemble_context(
        query=marker,
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
        query="今天几号",
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
