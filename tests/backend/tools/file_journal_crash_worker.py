"""Synthetic subprocess crash fixture; no user data, credentials or models."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from app.database import connect, init_db, now_iso
from app.tools.file_operations import FileOperationRequest, execute_file_batch
from app.workspace import file_journal


def main() -> None:
    action, workspace, identifier, phase, count_value = sys.argv[1:]
    count = int(count_value)
    root = Path(workspace)
    root.mkdir(exist_ok=True)
    init_db()
    with connect() as db:
        stamp = now_iso()
        db.execute("INSERT OR IGNORE INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(42,?,?,?,?,?)",
                   ("synthetic-crash", str(root), "full", stamp, stamp))
    if action == "inspect":
        print(json.dumps(file_journal.reconcile(identifier, workspace=str(root), conversation_id=42)))
        return

    if action == "claim":
        from app.tools.file_operations import _start_transaction
        print("READY", flush=True)
        sys.stdin.readline()
        try:
            _start_transaction(identifier, str(root), [], None, conversation_id=42,
                               request_hash="synthetic-request", plan_hash="synthetic-plan", source="api", mode="full")
            print(json.dumps({"claimed": True}))
        except file_journal.JournalError as exc:
            print(json.dumps({"claimed": False, "error_code": exc.code}))
        return

    if action == "submit":
        def wait_after_claim(current, *_):
            if current == "plan_prepared" and phase == "pause":
                print("READY", flush=True)
                sys.stdin.readline()
        file_journal.checkpoint = wait_after_claim
        operations = [FileOperationRequest("file.write", {"path": "once.txt", "content": "once", "expected_version_token": "missing"})]
        print(json.dumps(execute_file_batch(str(root), operations, operation_id=identifier, conversation_id=42)))
        return

    def crash_at(current: str, _transaction_id: str, index: int) -> None:
        if current == phase and index in {-1, 0}:
            os._exit(73)

    file_journal.checkpoint = crash_at
    if phase in {"compensation_started", "compensated"}:
        from app.tools.batch_grants import BatchGrant
        record = BatchGrant.record_change

        def fail_after_last_effect(grant, change_id, index):
            record(grant, change_id, index)
            if index == count - 1:
                raise PermissionError("synthetic failure after final effect")

        BatchGrant.record_change = fail_after_last_effect
    operations = []
    for index in range(count):
        base = index - index % 4
        source, copied, moved = f"file-{base}.txt", f"copy-{base}.txt", f"moved-{base}.txt"
        kind = index % 4
        if kind == 0:
            request = FileOperationRequest("file.write", {"path": source, "content": f"synthetic-{index}", "expected_version_token": "missing"})
        elif kind == 1:
            request = FileOperationRequest("file.copy", {"source": source, "destination": copied,
                "expected_version_token": f"batch:{base}", "expected_destination_version_token": "missing"})
        elif kind == 2:
            request = FileOperationRequest("file.move", {"source": copied, "destination": moved,
                "expected_version_token": f"batch:{base + 1}", "expected_destination_version_token": "missing"})
        else:
            request = FileOperationRequest("file.delete", {"path": source, "expected_version_token": f"batch:{base}"})
        operations.append(request)
    execute_file_batch(str(root), operations, operation_id=identifier, conversation_id=42)
    raise AssertionError("Requested crash phase was never reached")


if __name__ == "__main__":
    main()
