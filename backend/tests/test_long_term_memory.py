import uuid

import pytest
from fastapi.testclient import TestClient

from app.admin_action_grants import consume_admin_action_grant, issue_admin_action_grant
from app.long_term_memory import MemoryConflictError, create_memory, delete_memory, memory_history, retrieve_memories, update_memory
from app.main import app


def unique_content(label: str) -> str:
    return f"{label} {uuid.uuid4().hex}"


def authorization(operation: str, target_id: str, payload: dict):
    ui_session_id = f"test-{uuid.uuid4().hex}"
    grant = issue_admin_action_grant(
        operation=operation, target_id=target_id, payload=payload, ui_session_id=ui_session_id
    )
    return consume_admin_action_grant(
        grant["grant_token"],
        operation=operation,
        target_id=target_id,
        payload=payload,
        ui_session_id=ui_session_id,
    )


def test_memory_crud_lock_and_soft_delete() -> None:
    item = create_memory(
        memory_type="semantic",
        content=unique_content("管理员主要开发 Windows 桌面端"),
        source_type="user_confirmed",
        confidence=0.98,
        importance=0.9,
        user_confirmed=True,
        is_locked=True,
    )
    with pytest.raises(PermissionError):
        update_memory(item["id"], {"content": "unauthorized"}, authorization=None)
    changes = {"importance": 0.95}
    changed = update_memory(
        item["id"], changes, authorization=authorization("memory.update", item["id"], changes)
    )
    assert changed["importance"] == 0.95
    with pytest.raises(PermissionError):
        delete_memory(item["id"], authorization=None)
    assert delete_memory(
        item["id"], authorization=authorization("memory.delete", item["id"], {})
    )["deleted"] is True


def test_memory_retrieval_prioritizes_confirmed_relevant_memory() -> None:
    marker = uuid.uuid4().hex
    expected = create_memory(
        memory_type="procedural",
        content=f"修改 {marker} 项目前先阅读现有实现",
        source_type="user_confirmed",
        confidence=1,
        importance=1,
        user_confirmed=True,
    )
    results = retrieve_memories(marker, limit=3)
    assert results[0]["id"] == expected["id"]
    assert results[0]["retrieval_score"] > 0


def test_candidate_requires_confirmation_before_becoming_memory() -> None:
    with TestClient(app) as client:
        candidate = client.post(
            "/api/long-term-memories/candidates",
            json={
                "memory_type": "semantic",
                "content": unique_content("候选记忆"),
                "reason": "用户明确陈述",
                "confidence": 0.9,
                "importance": 0.8,
            },
        ).json()
        rejected = client.post(
            f"/api/long-term-memories/candidates/{candidate['id']}/decision",
            json={"accept": True},
        )
    assert rejected.status_code == 403


def test_admin_grant_is_payload_bound_and_single_use() -> None:
    ui_session_id = f"test-{uuid.uuid4().hex}"
    grant = issue_admin_action_grant(
        operation="memory.create",
        target_id="new",
        payload={"content": "approved"},
        ui_session_id=ui_session_id,
    )
    with pytest.raises(PermissionError):
        consume_admin_action_grant(
            grant["grant_token"],
            operation="memory.create",
            target_id="new",
            payload={"content": "tampered"},
            ui_session_id=ui_session_id,
        )
    consumed = consume_admin_action_grant(
        grant["grant_token"],
        operation="memory.create",
        target_id="new",
        payload={"content": "approved"},
        ui_session_id=ui_session_id,
    )
    assert consumed.operation == "memory.create"
    with pytest.raises(PermissionError):
        consume_admin_action_grant(
            grant["grant_token"],
            operation="memory.create",
            target_id="new",
            payload={"content": "approved"},
            ui_session_id=ui_session_id,
        )


def test_sensitive_content_is_flagged_automatically() -> None:
    item = create_memory(
        memory_type="semantic",
        content="临时 API Key 是 sk-test_DO_NOT_USE_000000000000",
        source_type="manual_entry",
    )
    assert item["is_sensitive"] is True


def test_new_confirmed_fact_supersedes_old_fact_without_deleting_history() -> None:
    subject = f"weight-{uuid.uuid4().hex}"
    old = create_memory(
        memory_type="semantic", content="管理员体重曾为 65kg", source_type="user_confirmed",
        user_confirmed=True, metadata={"subject": subject, "predicate": "current_weight"},
    )
    new = create_memory(
        memory_type="semantic", content="管理员当前体重为 60kg", source_type="user_confirmed",
        user_confirmed=True, metadata={"subject": subject, "predicate": "current_weight"},
    )
    history = memory_history(new["id"])
    assert new["supersedes_memory_id"] == old["id"]
    assert {item["status"] for item in history} >= {"active", "superseded"}


def test_locked_conflict_and_deleted_memory_cannot_be_restored_automatically() -> None:
    subject = f"locked-{uuid.uuid4().hex}"
    locked = create_memory(
        memory_type="semantic", content=unique_content("锁定事实"), source_type="user_confirmed",
        user_confirmed=True, is_locked=True, metadata={"subject": subject, "predicate": "preference"},
    )
    with pytest.raises(MemoryConflictError):
        create_memory(
            memory_type="semantic", content=unique_content("冲突事实"), source_type="user_confirmed",
            user_confirmed=True, metadata={"subject": subject, "predicate": "preference"},
        )
    transient = create_memory(memory_type="episodic", content=unique_content("待遗忘事件"))
    delete_memory(
        transient["id"], authorization=authorization("memory.delete", transient["id"], {})
    )
    with pytest.raises(MemoryConflictError):
        create_memory(memory_type="episodic", content=transient["content"], source_type="agent_inference")
    assert locked["is_locked"] is True
