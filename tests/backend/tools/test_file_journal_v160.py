from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest
from fastapi.testclient import TestClient

from app.database import connect, now_iso
from app.main import app
from app.tools.file_operations import FileOperationRequest, execute_file_batch
from app.workspace import file_journal
from app.sandbox import execute_tool


def conversation(root: Path) -> int:
    with connect() as db:
        stamp = now_iso()
        cursor = db.execute("INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                            ("file-journal-test", str(root), "full", stamp, stamp))
        return cursor.lastrowid


def requests(count: int) -> list[FileOperationRequest]:
    return [FileOperationRequest("file.write", {"path": f"file-{index}.txt", "content": f"sensitive-synthetic-body-{index}",
                                               "expected_version_token": "missing"}) for index in range(count)]


def test_preview_is_persisted_without_body_and_commit_is_idempotent(tmp_path: Path):
    conversation_id = conversation(tmp_path)
    operation_id = uuid.uuid4().hex
    args = dict(conversation_id=conversation_id, operation_id=operation_id)
    preview = execute_file_batch(str(tmp_path), requests(2), dry_run=True, **args)
    assert preview["success"], preview
    assert preview["operation_id"] == operation_id
    assert not list(tmp_path.glob("*.txt"))
    info = file_journal.detail(operation_id, workspace=str(tmp_path), conversation_id=conversation_id)
    assert info["status"] == "preview"
    assert len(info["steps"]) == 2
    assert "sensitive-synthetic-body" not in json.dumps(info)
    result = execute_file_batch(str(tmp_path), requests(2), expected_plan_hash=preview["plan_hash"], **args)
    assert result["success"], result
    before = [(p.name, p.stat().st_mtime_ns) for p in tmp_path.glob("*.txt")]
    repeated = execute_file_batch(str(tmp_path), requests(2), expected_plan_hash=preview["plan_hash"], **args)
    assert repeated["success"] and repeated["replayed_receipt"] and not repeated["executed"]
    assert before == [(p.name, p.stat().st_mtime_ns) for p in tmp_path.glob("*.txt")]
    info = file_journal.detail(operation_id, workspace=str(tmp_path), conversation_id=conversation_id)
    assert [step["state"] for step in info["steps"]] == ["committed", "committed"]
    assert "sensitive-synthetic-body" not in json.dumps(info)


def test_operation_id_rejects_changed_payload_scope_and_mode(tmp_path: Path):
    cid = conversation(tmp_path)
    operation_id = uuid.uuid4().hex
    preview = execute_file_batch(str(tmp_path), requests(1), dry_run=True, conversation_id=cid, operation_id=operation_id)
    for variants in (
        dict(conversation_id=conversation(tmp_path), mode="full", operations=requests(1)),
        dict(conversation_id=cid, mode="agent", operations=requests(1)),
        dict(conversation_id=cid, mode="full", operations=requests(2)),
    ):
        result = execute_file_batch(str(tmp_path), variants.pop("operations"), operation_id=operation_id,
                                    expected_plan_hash=preview["plan_hash"], **variants)
        assert result["error_code"] == "operation_id_conflict"
    assert not list(tmp_path.glob("*.txt"))


def test_plan_hash_mismatch_stops_before_effects(tmp_path: Path):
    result = execute_file_batch(str(tmp_path), requests(1), operation_id=uuid.uuid4().hex, expected_plan_hash="0" * 64)
    assert result["error_code"] == "plan_hash_conflict"
    assert not (tmp_path / "file-0.txt").exists()


class SimulatedProcessStop(BaseException):
    pass


@pytest.mark.parametrize("count", [1, 10, 50])
@pytest.mark.parametrize("phase,expected", [
    ("plan_prepared", "not_started"),
    ("backup_verified", "not_started"),
    ("effect_started", "needs_attention"),
    ("after_manifest_before_journal", "completed"),
    ("effect_observed", "completed"),
    ("committed", "completed"),
])
def test_interruption_records_are_explainable_and_never_replayed(tmp_path: Path, monkeypatch, count: int, phase: str, expected: str):
    cid = conversation(tmp_path)
    operation_id = uuid.uuid4().hex

    def crash_at(current, _transaction_id, index):
        if current == phase and index in {-1, 0}:
            raise SimulatedProcessStop()

    monkeypatch.setattr(file_journal, "checkpoint", crash_at)
    with pytest.raises(SimulatedProcessStop):
        execute_file_batch(str(tmp_path), requests(count), operation_id=operation_id, conversation_id=cid)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.txt")}
    monkeypatch.setattr(file_journal, "checkpoint", lambda *_: None)
    result = file_journal.reconcile(operation_id, workspace=str(tmp_path), conversation_id=cid)
    assert result["steps"][0]["observed_state"] == expected, result
    assert all(step["observed_state"] == "not_started" for step in result["steps"][1:])
    assert not result["automatic_replay"] and result["requires_new_authorization"]
    replay = execute_file_batch(str(tmp_path), requests(count), operation_id=operation_id, conversation_id=cid)
    assert replay["error_code"] == "operation_reconciliation_required"
    assert before == {p.name: p.read_bytes() for p in tmp_path.glob("*.txt")}


def test_reconcile_detects_external_edit_and_does_not_change_files(tmp_path: Path, monkeypatch):
    cid = conversation(tmp_path)
    operation_id = uuid.uuid4().hex

    def stop_after_effect(phase, *_):
        if phase == "after_manifest_before_journal":
            raise SimulatedProcessStop()

    monkeypatch.setattr(file_journal, "checkpoint", stop_after_effect)
    with pytest.raises(SimulatedProcessStop):
        execute_file_batch(str(tmp_path), requests(1), operation_id=operation_id, conversation_id=cid)
    (tmp_path / "file-0.txt").write_text("external", encoding="utf-8")
    result = file_journal.reconcile(operation_id, workspace=str(tmp_path), conversation_id=cid)
    assert result["status"] == "needs_attention"
    assert result["steps"][0]["reason"] == "workspace_drift"
    assert (tmp_path / "file-0.txt").read_text(encoding="utf-8") == "external"


def test_transaction_api_is_scoped_paginated_and_metadata_only(tmp_path: Path):
    cid = conversation(tmp_path)
    other = conversation(tmp_path)
    preview = execute_file_batch(str(tmp_path), requests(1), dry_run=True, conversation_id=cid)
    operation_id = preview["operation_id"]
    with TestClient(app) as client:
        listing = client.get(f"/api/file-transactions?conversation_id={cid}&limit=1").json()
        assert listing["total"] == 1 and listing["items"][0]["operation_id"] == operation_id
        assert client.get(f"/api/file-transactions/{operation_id}?conversation_id={other}").status_code == 404
        info = client.get(f"/api/file-transactions/{operation_id}?conversation_id={cid}")
        assert info.status_code == 200 and "sensitive-synthetic-body" not in info.text
        check = client.post(f"/api/file-transactions/{operation_id}/reconcile", json={"conversation_id": cid}).json()
        assert check["workspace_modified"] is False


def test_batch_restore_does_not_restore_other_batches_in_same_task(tmp_path: Path):
    cid = conversation(tmp_path)
    result = execute_file_batch(str(tmp_path), requests(2), conversation_id=cid, task_id="same-task")
    other = execute_file_batch(str(tmp_path), [FileOperationRequest("file.write", {
        "path": "unrelated.txt", "content": "unrelated", "expected_version_token": "missing"})],
                               conversation_id=cid, task_id="same-task")
    assert result["success"] and other["success"]
    restored = execute_tool(str(tmp_path), "full", "undo_file_batch", {"operation_id": result["operation_id"]},
                            conversation_id=cid)
    assert restored["success"], restored
    assert not (tmp_path / "file-0.txt").exists()
    assert not (tmp_path / "file-1.txt").exists()
    assert (tmp_path / "unrelated.txt").read_text(encoding="utf-8") == "unrelated"


def test_batch_restore_all_preflight_and_new_authorization(tmp_path: Path):
    cid = conversation(tmp_path)
    result = execute_file_batch(str(tmp_path), requests(2), conversation_id=cid)
    args = {"operation_id": result["operation_id"]}
    readonly = execute_tool(str(tmp_path), "readonly", "undo_file_batch", args, conversation_id=cid)
    ask = execute_tool(str(tmp_path), "ask", "undo_file_batch", args, conversation_id=cid)
    assert not readonly.get("success", False)
    assert ask["status"] == "confirmation_required"
    (tmp_path / "file-0.txt").write_text("external", encoding="utf-8")
    restored = execute_tool(str(tmp_path), "full", "undo_file_batch", args, conversation_id=cid)
    assert restored["error_code"] == "recovery_conflict"
    assert (tmp_path / "file-1.txt").exists()
    assert (tmp_path / "file-0.txt").read_text(encoding="utf-8") == "external"


def test_reconcile_restored_batch_checks_disk_and_rejects_old_missing_proof(tmp_path: Path):
    cid = conversation(tmp_path)
    result = execute_file_batch(str(tmp_path), requests(1), conversation_id=cid)
    restored = execute_tool(str(tmp_path), "full", "undo_file_batch", {"operation_id": result["operation_id"]}, conversation_id=cid)
    assert restored["success"]
    checked = file_journal.reconcile(result["operation_id"], workspace=str(tmp_path), conversation_id=cid)
    assert checked["steps"][0]["observed_state"] == "compensated"
    (tmp_path / "file-0.txt").write_text("external", encoding="utf-8")
    checked = file_journal.reconcile(result["operation_id"], workspace=str(tmp_path), conversation_id=cid)
    assert checked["status"] == "needs_attention"
    assert checked["steps"][0]["reason"] == "workspace_drift"
    record = tmp_path / ".agent-backups" / result["change_ids"][0] / "manifest.json"
    manifest = json.loads(record.read_text(encoding="utf-8"))
    del manifest["recovery"]["after"]
    record.write_text(json.dumps(manifest), encoding="utf-8")
    checked = file_journal.reconcile(result["operation_id"], workspace=str(tmp_path), conversation_id=cid)
    assert checked["steps"][0]["observed_state"] == "needs_attention"
    assert (tmp_path / "file-0.txt").read_text(encoding="utf-8") == "external"


def test_reconcile_same_path_reverse_compensation_uses_latest_event(tmp_path: Path):
    cid = conversation(tmp_path)
    plan = [FileOperationRequest("file.write", {"path": "a.txt", "content": "first", "expected_version_token": "missing"}),
            FileOperationRequest("file.write", {"path": "a.txt", "content": "second", "expected_version_token": "batch:0"})]
    result = execute_file_batch(str(tmp_path), plan, conversation_id=cid)
    assert result["success"], result
    checked = file_journal.reconcile(result["operation_id"], workspace=str(tmp_path), conversation_id=cid)
    assert all(step["observed_state"] == "completed" for step in checked["steps"])
    restored = execute_tool(str(tmp_path), "full", "undo_file_batch", {"operation_id": result["operation_id"]}, conversation_id=cid)
    assert restored["success"], restored
    checked = file_journal.reconcile(result["operation_id"], workspace=str(tmp_path), conversation_id=cid)
    assert all(step["observed_state"] == "compensated" for step in checked["steps"])
    (tmp_path / "a.txt").write_text("external", encoding="utf-8")
    assert file_journal.reconcile(result["operation_id"], workspace=str(tmp_path), conversation_id=cid)["status"] == "needs_attention"


@pytest.mark.parametrize("damage", ["shape", "path", "backup"])
def test_reconcile_rejects_malformed_or_plan_mismatched_evidence(tmp_path: Path, damage: str):
    cid = conversation(tmp_path)
    (tmp_path / "a.txt").write_text("before", encoding="utf-8")
    from app.sandbox import file_version_token
    result = execute_file_batch(str(tmp_path), [FileOperationRequest("file.write", {
        "path": "a.txt", "content": "after", "expected_version_token": file_version_token(tmp_path / "a.txt")})], conversation_id=cid)
    record = tmp_path / ".agent-backups" / result["change_ids"][0] / "manifest.json"
    manifest = json.loads(record.read_text(encoding="utf-8"))
    if damage == "shape":
        manifest = []
    elif damage == "path":
        manifest["entries"][0]["path"] = "another.txt"
    else:
        (record.parent / "0.bak").write_text("damaged", encoding="utf-8")
    record.write_text(json.dumps(manifest), encoding="utf-8")
    checked = file_journal.reconcile(result["operation_id"], workspace=str(tmp_path), conversation_id=cid)
    assert checked["status"] == "needs_attention"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "after"


@pytest.mark.parametrize("count", [1, 10, 50])
@pytest.mark.parametrize("phase,expected", [
    ("backup_verified", "not_started"),
    ("effect_started", "needs_attention"),
    ("after_manifest_before_journal", "completed"),
    ("committed", "completed"),
    ("compensation_started", "completed"),
    ("compensated", "compensated"),
])
def test_real_process_exit_and_fresh_process_reconciliation(tmp_path: Path, count: int, phase: str, expected: str):
    worker = Path(__file__).with_name("file_journal_crash_worker.py")
    root = tmp_path / "workspace"
    isolated = tmp_path / "application-data"
    isolated.mkdir()
    env = {**os.environ, "AGENT_DATA_ROOT": str(isolated), "AGENT_DESKTOP_DATA_DIRECTORY": str(isolated),
           "AGENT_DATABASE_PATH": str(isolated / "synthetic.db"), "AGENT_LOG_PATH": str(isolated / "synthetic.log")}
    identifier = uuid.uuid4().hex
    args = [str(worker), "crash", str(root), identifier, phase, str(count)]
    stopped = subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, timeout=45)
    assert stopped.returncode == 73, stopped.stderr
    before = {path.name: path.read_bytes() for path in root.glob("*.txt")}
    args[1] = "inspect"
    observed = subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, timeout=45)
    assert observed.returncode == 0, observed.stderr
    result = json.loads(observed.stdout.strip().splitlines()[-1])
    assert result["steps"][0]["observed_state"] == expected
    assert len(result["steps"]) == count
    assert not result["automatic_replay"] and not result["workspace_modified"]
    assert before == {path.name: path.read_bytes() for path in root.glob("*.txt")}


def test_double_submit_and_running_reconcile_are_blocked(tmp_path: Path, monkeypatch):
    cid = conversation(tmp_path)
    identifier = uuid.uuid4().hex
    reached, release = threading.Event(), threading.Event()

    def pause(phase, *_):
        if phase == "effect_started":
            reached.set()
            assert release.wait(10)

    monkeypatch.setattr(file_journal, "checkpoint", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_file_batch, str(tmp_path), requests(1), operation_id=identifier, conversation_id=cid)
        try:
            assert reached.wait(10)
            duplicate = execute_file_batch(str(tmp_path), requests(1), operation_id=identifier, conversation_id=cid)
            assert duplicate["error_code"] == "operation_in_progress"
            with pytest.raises(file_journal.JournalError, match="仍在"):
                file_journal.reconcile(identifier, workspace=str(tmp_path), conversation_id=cid)
        finally:
            release.set()
        assert future.result(timeout=10)["success"]
    assert len(list((tmp_path / ".agent-backups").glob("*/manifest.json"))) == 1


def test_batch_revocation_before_second_step_never_escalates_compensation(tmp_path: Path, monkeypatch):
    cid = conversation(tmp_path)

    def revoke(phase, _id, index):
        if phase == "committed" and index == 0:
            with connect() as db:
                db.execute("UPDATE conversations SET permission_mode='readonly' WHERE id=?", (cid,))

    monkeypatch.setattr(file_journal, "checkpoint", revoke)
    result = execute_file_batch(str(tmp_path), requests(2), conversation_id=cid)
    assert not result["success"] and not result["rolled_back"]
    assert result["rollback"][0]["error_code"] == "batch_context_changed"
    assert (tmp_path / "file-0.txt").exists()
    assert not (tmp_path / "file-1.txt").exists()


@pytest.mark.parametrize("mode", ["claim", "submit"])
def test_two_real_processes_share_atomic_claim_and_no_duplicate_effect(tmp_path: Path, mode: str):
    worker = Path(__file__).with_name("file_journal_crash_worker.py")
    root = tmp_path / "workspace"
    root.mkdir()
    isolated = tmp_path / "application-data"
    isolated.mkdir()
    env = {**os.environ, "AGENT_DATA_ROOT": str(isolated), "AGENT_DESKTOP_DATA_DIRECTORY": str(isolated),
           "AGENT_DATABASE_PATH": str(isolated / "synthetic.db"), "AGENT_LOG_PATH": str(isolated / "synthetic.log")}
    identifier = uuid.uuid4().hex
    command = [sys.executable, str(worker), mode, str(root), identifier, "pause", "1"]
    processes = []
    try:
        first = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(first)
        assert first.stdout.readline().strip() == "READY"
        if mode == "claim":
            second = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(second)
            assert second.stdout.readline().strip() == "READY"
            first.stdin.write("go\n"); first.stdin.flush()
            second.stdin.write("go\n"); second.stdin.flush()
            responses = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                assert process.returncode == 0, stderr
                responses.append(json.loads(stdout.strip().splitlines()[-1]))
            assert sum(result["claimed"] for result in responses) == 1
        else:
            command[-2] = "no-pause"
            duplicate = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            assert duplicate.returncode == 0, duplicate.stderr
            result = json.loads(duplicate.stdout.strip().splitlines()[-1])
            assert result["error_code"] == "operation_in_progress"
            stdout, stderr = first.communicate("go\n", timeout=30)
            assert first.returncode == 0, stderr
            assert json.loads(stdout.strip().splitlines()[-1])["success"]
            assert (root / "once.txt").read_text(encoding="utf-8") == "once"
            assert len(list((root / ".agent-backups").glob("*/manifest.json"))) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
