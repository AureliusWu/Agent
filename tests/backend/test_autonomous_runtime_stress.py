import json
import uuid
from pathlib import Path

from app.database import connect, now_iso
from app.execution_segments import SegmentSnapshot, finish_segment, start_segment
from app.recovery import create_checkpoint, load_checkpoint


SEGMENT_COUNT = 1_000
MODEL_LOOP_COUNT = 10_000
TOOL_CALL_COUNT = 50_000
CHECKPOINT_INTERVAL = 10


def _create_running_task(workspace: Path) -> tuple[int, str]:
    stamp = now_iso()
    task_id = uuid.uuid4().hex
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title, workspace, permission_mode, created_at, updated_at) "
            "VALUES(?,?,?,?,?)",
            ("synthetic stress", str(workspace), "agent", stamp, stamp),
        )
        conversation_id = int(cursor.lastrowid)
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, current_phase, current_step, "
            "created_at, updated_at, started_at, resumable) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "synthetic durable runtime stress", "analysis", "stress", stamp, stamp, stamp, 1),
        )
    return conversation_id, task_id


def test_required_scale_persists_without_fixed_limit_or_duplicate_consumption(tmp_path: Path) -> None:
    conversation_id, task_id = _create_running_task(tmp_path)
    evidence = {"workspace_hash": "synthetic-workspace", "git_status": "", "snapshot": {"kind": "synthetic"}}

    for index in range(1, SEGMENT_COUNT + 1):
        snapshot = SegmentSnapshot(
            phase="analysis",
            completed_steps=(f"segment-{index - 1}",),
            pending_steps=(f"segment-{index}",),
            tool_result_refs=(f"receipt-{index * 50}",),
            context_summary=f"synthetic compaction {index}",
            input_tokens=index * 30,
            output_tokens=index * 10,
            total_tokens=index * 40,
            model_calls=index * 10,
            tool_calls=index * 50,
        )
        segment = start_segment(task_id, "stress_boundary", snapshot)
        finish_segment(segment["id"], task_id, "completed", "stress_boundary", snapshot)
        if index % CHECKPOINT_INTERVAL == 0:
            create_checkpoint(
                task_id,
                str(tmp_path),
                "analysis",
                "synthetic_compaction",
                {
                    "completed_steps": [f"segment-{index}"],
                    "pending_steps": [f"segment-{index + 1}"],
                    "working_memory": {"last_segment": index},
                    "tool_result_refs": [f"receipt-{index * 50}"],
                },
                workspace_evidence_override=evidence,
            )

    stamp = now_iso()
    model_rows = [
        (conversation_id, task_id, "synthetic", "controlled-provider", stamp, stamp, 0, 3, 1, 4, 1, "analysis")
        for _ in range(MODEL_LOOP_COUNT)
    ]
    tool_rows = [
        (
            conversation_id,
            task_id,
            "builtin",
            "low",
            f"synthetic-tool-{index:05d}",
            1,
            "list_files",
            "ok",
            json.dumps({"path": f"batch-{index % 1_000:04d}"}),
            json.dumps({"success": True, "status": "ok", "receipt": {"receipt_version": 2}}),
            stamp,
            stamp,
            0,
        )
        for index in range(TOOL_CALL_COUNT)
    ]
    with connect() as db:
        db.executemany(
            "INSERT INTO model_runs(conversation_id, task_id, provider, model, started_at, finished_at, duration_ms, "
            "input_tokens, output_tokens, total_tokens, success, phase) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            model_rows,
        )
        db.executemany(
            "INSERT INTO tool_runs(conversation_id, task_id, source, risk, execution_id, confirmed, tool, status, "
            "input, output, started_at, finished_at, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tool_rows,
        )
        db.execute(
            "UPDATE agent_tasks SET status='waiting_provider', current_step='provider_wait', resumable=1, updated_at=? WHERE id=?",
            (stamp, task_id),
        )
        db.execute(
            "UPDATE agent_tasks SET status='recovering', current_step='recovering', resume_count=resume_count+1, updated_at=? WHERE id=?",
            (stamp, task_id),
        )
        db.execute(
            "UPDATE agent_tasks SET status='running', current_step='continued', model_calls=?, tool_calls=?, updated_at=? WHERE id=?",
            (MODEL_LOOP_COUNT, TOOL_CALL_COUNT, stamp, task_id),
        )

    with connect() as db:
        segment_stats = db.execute(
            "SELECT COUNT(*), MIN(sequence), MAX(sequence), SUM(status='running') FROM execution_segments WHERE task_id=?",
            (task_id,),
        ).fetchone()
        model_count = int(db.execute("SELECT COUNT(*) FROM model_runs WHERE task_id=?", (task_id,)).fetchone()[0])
        tool_count, unique_tool_count = db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT execution_id) FROM tool_runs WHERE task_id=?",
            (task_id,),
        ).fetchone()
        checkpoint_count = int(db.execute("SELECT COUNT(*) FROM task_checkpoints WHERE task_id=?", (task_id,)).fetchone()[0])
        task = dict(db.execute("SELECT status, current_step, model_calls, tool_calls, resume_count, resumable FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    assert tuple(segment_stats) == (SEGMENT_COUNT, 1, SEGMENT_COUNT, 0)
    assert model_count == MODEL_LOOP_COUNT
    assert (int(tool_count), int(unique_tool_count)) == (TOOL_CALL_COUNT, TOOL_CALL_COUNT)
    assert checkpoint_count == SEGMENT_COUNT // CHECKPOINT_INTERVAL
    assert load_checkpoint(task_id)["state"]["working_memory"] == {"last_segment": SEGMENT_COUNT}
    assert task == {
        "status": "running",
        "current_step": "continued",
        "model_calls": MODEL_LOOP_COUNT,
        "tool_calls": TOOL_CALL_COUNT,
        "resume_count": 1,
        "resumable": 1,
    }
