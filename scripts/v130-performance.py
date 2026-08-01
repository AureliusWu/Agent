from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "v130-evidence" / "performance"
OUTPUT.mkdir(parents=True, exist_ok=True)
runtime = Path(tempfile.mkdtemp(prefix="siyi-v130-perf-"))
os.environ["AGENT_DATABASE_PATH"] = str(runtime / "agent.db")
sys.path.insert(0, str(ROOT / "siyi"))

from app.database import connect, database_status, init_db, now_iso  # noqa: E402
from app.process_supervisor import terminate_process_tree  # noqa: E402
from app.sandbox import file_version_token  # noqa: E402
from app.tools.file_operations import FileOperationRequest, execute_file_operation  # noqa: E402
from app.workspace.snapshots import create_security_snapshot, restore_security_snapshot  # noqa: E402


def measure(function):
    started = time.perf_counter()
    value = function()
    return round((time.perf_counter() - started) * 1000, 3), value


db_ms, _ = measure(init_db)
workspace = runtime / "workspace"
workspace.mkdir()
for index in range(1000):
    (workspace / f"file-{index:04}.txt").write_text(f"value {index}", encoding="utf-8")

scan_100_ms, scanned = measure(lambda: list(workspace.glob("file-00*.txt"))[:100])


def move_100():
    target = workspace / "moved"
    target.mkdir()
    results = []
    for index in range(100):
        source = workspace / f"file-{index:04}.txt"
        results.append(execute_file_operation(str(workspace), FileOperationRequest("file.move", {"source": source.name, "destination": f"moved/{source.name}", "expected_version_token": file_version_token(source), "expected_destination_version_token": "missing"})))
    if not all(item["success"] for item in results):
        raise RuntimeError("100-file move failed")
    return results


move_100_ms, moves = measure(move_100)
snapshot_1000_ms, snapshot = measure(lambda: create_security_snapshot(str(workspace), reason="v13 performance 1000 files"))
restore_1000_ms, restored_snapshot = measure(lambda: restore_security_snapshot(str(workspace), snapshot["id"]))


def rollback_moves():
    restored = [execute_file_operation(str(workspace), FileOperationRequest("file.restore", {"change_id": item["change_id"]})) for item in reversed(moves)]
    if not all(item["success"] for item in restored):
        raise RuntimeError("100-file rollback failed")
    return restored


rollback_100_ms, _ = measure(rollback_moves)
ten_mb = "x" * (10 * 1024 * 1024)
write_10mb_ms, write_result = measure(lambda: execute_file_operation(str(workspace), FileOperationRequest("file.write", {"path": "ten-megabytes.txt", "content": ten_mb, "expected_version_token": "missing"})))
if not write_result["success"] or (workspace / "ten-megabytes.txt").stat().st_size != len(ten_mb):
    raise RuntimeError("10 MiB atomic write failed")
conflict_1000_ms, tokens = measure(lambda: [file_version_token(path) for path in sorted(workspace.glob("file-*.txt"))])


def insert_receipts():
    stamp = now_iso()
    with connect() as db:
        conversation_id = db.execute("INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)", ("perf", str(workspace), "full", stamp, stamp)).lastrowid
        task_id = uuid.uuid4().hex
        db.execute("INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)", (task_id, conversation_id, "completed", "receipt benchmark", stamp, stamp))
        db.executemany(
            "INSERT INTO tool_receipts(receipt_id,task_id,tool_call_id,tool_name,status,receipt_json,created_at) VALUES(?,?,?,?,?,?,?)",
            ((uuid.uuid4().hex, task_id, f"call-{index}", "read_file", "completed", '{"success":true}', stamp) for index in range(1000)),
        )
    return task_id


receipts_1000_ms, receipt_task_id = measure(insert_receipts)


def query_receipts():
    with connect() as db:
        return db.execute("SELECT receipt_id FROM tool_receipts WHERE task_id=?", (receipt_task_id,)).fetchall()


receipt_query_1000_ms, receipt_rows = measure(query_receipts)

process_started = time.perf_counter()
flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], creationflags=flags, start_new_session=os.name != "nt")
process_start_ms = round((time.perf_counter() - process_started) * 1000, 3)
stop_ms, stopped = measure(lambda: terminate_process_tree(process.pid))
if not stopped:
    raise RuntimeError("process stop failed")

measurements = {
    "database_migration": db_ms,
    "scan_100_files": scan_100_ms,
    "move_100_files": move_100_ms,
    "snapshot_1000_files": snapshot_1000_ms,
    "restore_1000_files": restore_1000_ms,
    "rollback_100_moves": rollback_100_ms,
    "atomic_write_10mib": write_10mb_ms,
    "conflict_scan_1000_files": conflict_1000_ms,
    "insert_1000_receipts": receipts_1000_ms,
    "query_1000_receipts": receipt_query_1000_ms,
    "process_start": process_start_ms,
    "process_tree_stop": stop_ms,
}
thresholds = {
    "scan_100_files": 10,
    "move_100_files": 2500,
    "snapshot_1000_files": 4000,
    "restore_1000_files": 7000,
    "rollback_100_moves": 2500,
    "atomic_write_10mib": 150,
    "conflict_scan_1000_files": 500,
    "process_tree_stop": 500,
}
checks = {name: {"actual_ms": measurements[name], "limit_ms": limit, "passed": measurements[name] <= limit} for name, limit in thresholds.items()}
report = {
    "schema_version": 1,
    "target_version": "13.0.0",
    "recorded_at": now_iso(),
    "status": "PASS" if all(item["passed"] for item in checks.values()) else "FAIL",
    "measurements_ms": measurements,
    "checks": checks,
    "observations": {"scanned": len(scanned), "conflict_tokens": len(tokens), "receipt_rows": len(receipt_rows), "snapshot_id": snapshot["id"], "snapshot_restore": restored_snapshot, "database": database_status()},
}
(OUTPUT / "performance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["status"] == "PASS" else 2)
