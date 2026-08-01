import json
import uuid

import pytest

from app.database import connect, now_iso
from app.providers.policy import classify_local_capability, create_provider_policy
from app.runtime.professional_orchestration import recovery_decision, review_execution_scope, send_role_message


@pytest.fixture
def task_factory():
    def create() -> str:
        stamp = now_iso()
        task_id = uuid.uuid4().hex
        with connect() as db:
            conversation_id = db.execute(
                "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("professional", "", "ask", stamp, stamp),
            ).lastrowid
            db.execute(
                "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (task_id, conversation_id, "running", "professional task", stamp, stamp),
            )
        return task_id

    return create


def test_structured_role_message_rejects_invalid_transition(tmp_path) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        send_role_message("missing", "reviewer", "planner", "review", {"verdict": "reject"})


def test_reviewer_detects_out_of_scope_receipt(task_factory) -> None:
    task_id = task_factory()
    with connect() as db:
        db.execute(
            "INSERT INTO tool_receipts(receipt_id,task_id,tool_call_id,tool_name,status,receipt_json,created_at) VALUES(?,?,?,?,?,?,?)",
            ("receipt-scope", task_id, "call", "write_file", "completed", json.dumps({"affected_paths": ["outside.txt"]}), now_iso()),
        )
    review = review_execution_scope(task_id, ("allowed.txt",))
    assert review["status"] == "rejected"
    assert review["out_of_scope"] == ["outside.txt"]


def test_recovery_is_finite_and_prefers_rollback_when_possible() -> None:
    assert recovery_decision(attempt=0, max_attempts=2, reversible=True, evidence_changed=False) == "retry"
    assert recovery_decision(attempt=1, max_attempts=2, reversible=True, evidence_changed=True) == "repair"
    assert recovery_decision(attempt=2, max_attempts=2, reversible=True, evidence_changed=True) == "rollback"
    assert recovery_decision(attempt=2, max_attempts=2, reversible=False, evidence_changed=True) == "block"


def test_local_provider_policy_has_no_silent_paid_fallback(task_factory) -> None:
    task_id = task_factory()
    policy = create_provider_policy("ollama", "qwen3:4b", task_id=task_id)
    assert not policy.allow_paid_fallback
    with pytest.raises(ValueError, match="silently"):
        create_provider_policy("ollama", "qwen3:4b", task_id=task_id, fallback_order=("deepseek",))
    assert classify_local_capability("chat") == "FULL"
    assert classify_local_capability("tool_calling") == "PARTIAL"
    assert classify_local_capability("vision") == "UNSUPPORTED"
