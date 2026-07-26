import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.context.compiler import compile_task_context, task_context_debug
from app.database import connect, now_iso
from app.main import app


def _task(tmp_path: Path) -> tuple[int, str]:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "Context v2", str(tmp_path), "ask", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "完成任务", stamp, stamp),
        )
    return conversation_id, task_id


def test_context_compiler_preserves_monotonic_state_across_recompilation(tmp_path: Path) -> None:
    _conversation_id, task_id = _task(tmp_path)
    first = compile_task_context(
        task_id,
        current={"user_task": "完成任务", "phase": "execution", "step": "write", "modified_files": ["a.py"]},
        working={
            "goal": "完成任务",
            "completed_steps": ["inspect"],
            "pending_steps": ["write", "verify"],
            "constraints": ["只修改 a.py"],
            "verification": {"status": "pending"},
        },
        decisions=("沿用现有模块边界",),
    )
    second = compile_task_context(
        task_id,
        current={"phase": "verification", "step": "test", "modified_files": []},
        working={
            "completed_steps": ["write"],
            "pending_steps": ["verify"],
            "constraints": [],
            "verification": {"status": "running"},
        },
        decisions=("沿用现有模块边界",),
    )

    assert second.revision > first.revision
    assert second.state["goal"] == "完成任务"
    assert second.state["completed_steps"] == ["inspect", "write"]
    assert second.state["modified_files"] == ["a.py"]
    assert second.state["constraints"] == ["只修改 a.py"]
    assert second.state["pending_steps"] == ["verify"]
    assert len(second.decisions) == 1


def test_context_compiler_uses_stable_receipt_and_verification_references(tmp_path: Path) -> None:
    conversation_id, task_id = _task(tmp_path)
    stamp = now_iso()
    receipt = {
        "receipt": {
            "receipt_version": 2,
            "operation_kind": "mutation",
            "change_id": "change-1",
            "error_fingerprint": None,
        }
    }
    report = {"evidence_fingerprint": "verify-fingerprint"}
    with connect() as db:
        tool_id = db.execute(
            "INSERT INTO tool_runs(conversation_id,task_id,tool,status,output,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
            (conversation_id, task_id, "write_file", "ok", json.dumps(receipt), stamp, stamp),
        ).lastrowid
        verification_id = db.execute(
            "INSERT INTO task_verifications(task_id,status,summary,report,created_at) VALUES(?,?,?,?,?)",
            (task_id, "passed", "通过", json.dumps(report), stamp),
        ).lastrowid

    compiled = compile_task_context(
        task_id,
        current={"user_task": "完成任务", "phase": "finalization", "step": "done"},
        working={"goal": "完成任务", "verification": {"status": "passed"}},
    )
    references = {item["ref"]: item for item in compiled.evidence_references}

    assert references[f"tool_run:{tool_id}"]["change_id"] == "change-1"
    assert references[f"verification:{verification_id}"]["evidence_fingerprint"] == "verify-fingerprint"
    assert "write_file" in compiled.text


def test_context_debug_endpoint_returns_structure_without_compiled_prompt(tmp_path: Path) -> None:
    _conversation_id, task_id = _task(tmp_path)
    compile_task_context(
        task_id,
        current={"user_task": "检查调试", "phase": "analysis", "step": "inspect"},
        working={"goal": "检查调试", "pending_steps": ["inspect"]},
    )

    direct = task_context_debug(task_id)
    with TestClient(app) as client:
        response = client.get(f"/api/tasks/{task_id}/context-debug")

    assert response.status_code == 200
    assert response.json()["state_fingerprint"] == direct["state_fingerprint"]
    assert "text" not in response.json()
    assert response.json()["compiler_version"] == 3


def test_typed_context_does_not_promote_inference_to_fact(tmp_path: Path) -> None:
    _conversation_id, task_id = _task(tmp_path)
    first = compile_task_context(
        task_id,
        current={"user_task": "分析故障", "phase": "analysis", "step": "inspect"},
        working={
            "goal": "分析故障",
            "typed_context": [
                {
                    "type": "INFERENCE",
                    "content": "缓存可能导致旧结果",
                    "source_ref": "model:analysis-1",
                }
            ],
        },
    )
    second = compile_task_context(
        task_id,
        current={"phase": "analysis", "step": "recheck"},
        working={
            "typed_context": [
                {
                    "type": "FACT",
                    "content": "缓存可能导致旧结果",
                    "source_ref": "model:analysis-1",
                }
            ],
        },
    )
    matching = [
        item
        for item in second.state["typed_context"]
        if item["content"] == "缓存可能导致旧结果"
    ]
    assert len(matching) == 1
    assert matching[0]["type"] == "INFERENCE"
    assert second.revision >= first.revision
