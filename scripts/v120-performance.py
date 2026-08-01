from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "v120-evidence"
OUTPUT.mkdir(parents=True, exist_ok=True)
runtime = Path(tempfile.mkdtemp(prefix="siyi-v120-perf-"))
os.environ["AGENT_DATABASE_PATH"] = str(runtime / "agent.db")
sys.path.insert(0, str(ROOT / "siyi"))

from app.database import database_status, init_db, now_iso  # noqa: E402
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
snapshot_100_ms, snapshot = measure(lambda: create_security_snapshot(str(workspace), reason="v12 performance 100 files"))
rollback_snapshot_ms, restored_snapshot = measure(lambda: restore_security_snapshot(str(workspace), snapshot["id"]))


def rollback_moves():
    restored = []
    for item in reversed(moves):
        restored.append(execute_file_operation(str(workspace), FileOperationRequest("file.restore", {"change_id": item["change_id"]})))
    if not all(item["success"] for item in restored):
        raise RuntimeError("100-file rollback failed")
    return restored


rollback_100_ms, _ = measure(rollback_moves)
ten_mb = "x" * (10 * 1024 * 1024)
write_10mb_ms, write_result = measure(lambda: execute_file_operation(str(workspace), FileOperationRequest("file.write", {"path": "ten-megabytes.txt", "content": ten_mb, "expected_version_token": "missing"})))
if not write_result["success"] or (workspace / "ten-megabytes.txt").stat().st_size != len(ten_mb):
    raise RuntimeError("10 MB atomic write failed")
conflict_scan_1000_ms, tokens = measure(lambda: [file_version_token(path) for path in sorted(workspace.glob("file-*.txt"))])

process_started = time.perf_counter()
flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], creationflags=flags, start_new_session=os.name != "nt")
process_start_ms = round((time.perf_counter() - process_started) * 1000, 3)
stop_ms, stopped = measure(lambda: terminate_process_tree(process.pid))
if not stopped:
    raise RuntimeError("process stop failed")

report = {
    "schema_version": 1,
    "target_version": "12.0.0",
    "recorded_at": now_iso(),
    "runtime_directory": str(runtime),
    "status": "PASS",
    "measurements_ms": {
        "database_migration": db_ms,
        "scan_100_files": scan_100_ms,
        "move_100_files": move_100_ms,
        "snapshot_1000_files": snapshot_100_ms,
        "restore_snapshot": rollback_snapshot_ms,
        "rollback_100_moves": rollback_100_ms,
        "atomic_write_10mb": write_10mb_ms,
        "conflict_scan_1000_files": conflict_scan_1000_ms,
        "process_start": process_start_ms,
        "process_tree_stop": stop_ms,
    },
    "observations": {"scanned": len(scanned), "conflict_tokens": len(tokens), "snapshot_id": snapshot["id"], "snapshot_restore": restored_snapshot, "database": database_status()},
    "not_measured": ["frontend_startup", "sidecar_ready", "first_model_request", "streaming_log_latency", "long_task_memory_growth", "provider_cpu_gpu_memory"],
}
(OUTPUT / "performance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report["measurements_ms"], ensure_ascii=False))
