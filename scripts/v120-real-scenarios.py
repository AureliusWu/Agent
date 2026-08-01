from __future__ import annotations

import hashlib
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
runtime = Path(tempfile.mkdtemp(prefix="siyi-v120-real-"))
os.environ["AGENT_DATABASE_PATH"] = str(runtime / "agent.db")
sys.path.insert(0, str(ROOT / "siyi"))

from app.database import connect, init_db, now_iso  # noqa: E402
from app.process_supervisor import _process_identity, terminate_process_tree  # noqa: E402
from app.sandbox import file_version_token  # noqa: E402
from app.tools.file_operations import FileOperationRequest, execute_file_batch, execute_file_operation  # noqa: E402


def request(operation: str, **arguments: object) -> FileOperationRequest:
    return FileOperationRequest(operation, arguments)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def scenario(name: str, function) -> dict[str, object]:
    started = time.perf_counter()
    try:
        evidence = function()
        return {"name": name, "status": "PASS", "duration_ms": round((time.perf_counter() - started) * 1000), "evidence": evidence}
    except Exception as exc:
        return {"name": name, "status": "FAIL", "duration_ms": round((time.perf_counter() - started) * 1000), "error": f"{type(exc).__name__}: {exc}"}


def file_organization() -> dict[str, object]:
    workspace = runtime / "organize"
    (workspace / "images").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "photo.png").write_bytes(b"png")
    (workspace / "notes.md").write_text("# Notes\n", encoding="utf-8")
    (workspace / "images" / "photo.png").write_bytes(b"existing")
    existing_hash = sha(workspace / "images" / "photo.png")
    conflict = execute_file_batch(
        str(workspace),
        [request("file.move", source="photo.png", destination="images/photo.png", expected_version_token=file_version_token(workspace / "photo.png"), expected_destination_version_token="missing")],
    )
    require(not conflict["success"] and conflict["error_code"] == "batch_preflight_failed", "duplicate target was not rejected")
    moved = execute_file_batch(
        str(workspace),
        [request("file.move", source="notes.md", destination="docs/notes.md", expected_version_token=file_version_token(workspace / "notes.md"), expected_destination_version_token="missing")],
    )
    require(moved["success"], "markdown move failed")
    require(existing_hash == sha(workspace / "images" / "photo.png"), "duplicate target was overwritten")
    change_id = moved["results"][0]["change_id"]
    restored = execute_file_operation(str(workspace), request("file.restore", change_id=change_id))
    require(restored["success"] and (workspace / "notes.md").is_file(), "move undo failed")
    return {"conflict": conflict, "committed_batch": moved["batch_id"], "undo": restored, "final_files": sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file() and ".agent-backups" not in p.parts)}


def markdown_batch() -> dict[str, object]:
    workspace = runtime / "markdown"
    workspace.mkdir()
    before: dict[str, str] = {}
    operations = []
    for index in range(3):
        path = workspace / f"doc-{index}.md"
        path.write_text(f"# old {index}\r\nbody {index}\r\n", encoding="utf-8", newline="")
        before[path.name] = sha(path)
        operations.append(request("file.write", path=path.name, content=f"# New {index}\r\nbody {index}\r\n", expected_version_token=file_version_token(path), encoding="utf-8"))
    result = execute_file_batch(str(workspace), operations)
    require(result["success"], "markdown batch failed")
    require(all((workspace / f"doc-{i}.md").read_bytes().count(b"\r\n") == 2 for i in range(3)), "line endings changed")
    change_ids = [entry["change_id"] for entry in result["results"]]
    for change_id in reversed(change_ids):
        require(execute_file_operation(str(workspace), request("file.restore", change_id=change_id))["success"], "markdown restore failed")
    after = {path.name: sha(path) for path in workspace.glob("*.md")}
    require(before == after, "restored hashes differ")
    return {"batch_id": result["batch_id"], "change_ids": change_ids, "before_sha256": before, "restored_sha256": after}


def safe_delete() -> dict[str, object]:
    workspace = runtime / "delete"
    workspace.mkdir()
    target = workspace / "temporary.log"
    target.write_text("temporary", encoding="utf-8")
    result = execute_file_operation(str(workspace), request("file.delete", path=target.name, expected_version_token=file_version_token(target)))
    require(result["success"] and result["recoverable"] and not target.exists(), "safe delete failed")
    manifest = workspace / result["trash_path"] / "manifest.json"
    require(manifest.is_file(), "trash manifest missing")
    restored = execute_file_operation(str(workspace), request("file.restore", change_id=result["change_id"]))
    require(restored["success"] and target.read_text(encoding="utf-8") == "temporary", "delete restore failed")
    return {"trash_id": result["trash_id"], "trash_path": result["trash_path"], "manifest_existed": True, "restored": restored}


def code_fix() -> dict[str, object]:
    workspace = runtime / "code-fix"
    workspace.mkdir()
    program = workspace / "calc.py"
    program.write_text("def add(a, b):\n    return a - b\n\nassert add(2, 3) == 5\n", encoding="utf-8")
    before = subprocess.run([sys.executable, str(program)], capture_output=True, text=True, check=False)
    require(before.returncode != 0, "bug did not reproduce")
    fixed = execute_file_operation(str(workspace), request("file.write", path="calc.py", content="def add(a, b):\n    return a + b\n\nassert add(2, 3) == 5\n", expected_version_token=file_version_token(program)))
    require(fixed["success"], "code fix write failed")
    after = subprocess.run([sys.executable, str(program)], capture_output=True, text=True, check=False)
    require(after.returncode == 0, "fixed test did not pass")
    return {"reproduction_exit_code": before.returncode, "fix_change_id": fixed["change_id"], "verification_exit_code": after.returncode}


def reliable_stop() -> dict[str, object]:
    workspace = runtime / "stop"
    workspace.mkdir()
    child_file = workspace / "child.pid"
    parent_code = "import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(120)"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(child_file)], creationflags=flags, start_new_session=os.name != "nt")
    deadline = time.time() + 10
    while not child_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    require(child_file.exists(), "child PID was not recorded")
    child_pid = int(child_file.read_text())
    started = time.perf_counter()
    stopped = terminate_process_tree(parent.pid)
    elapsed = round((time.perf_counter() - started) * 1000)
    try:
        parent.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    deadline = time.time() + 5
    while (_process_identity(parent.pid) is not None or _process_identity(child_pid) is not None) and time.time() < deadline:
        time.sleep(0.05)
    require(stopped and _process_identity(parent.pid) is None and _process_identity(child_pid) is None, "process tree survived stop")
    second = terminate_process_tree(parent.pid)
    return {"parent_pid": parent.pid, "child_pid": child_pid, "stop_ms": elapsed, "idempotent_second_stop": second, "parent_alive": False, "child_alive": False}


def crash_recovery() -> dict[str, object]:
    stamp = now_iso()
    with connect() as db:
        conversation_id = db.execute("INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)", ("recovery", str(runtime), "full", stamp, stamp)).lastrowid
        task_id = "v120-crash-task"
        db.execute("INSERT INTO agent_tasks(id,conversation_id,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (task_id, conversation_id, "recover", "running", stamp, stamp))
    init_db()
    with connect() as db:
        row = db.execute("SELECT status,resumable FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        transition = db.execute("SELECT from_status,to_status,trigger_source FROM task_transitions WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    require(row["status"] == "interrupted" and row["resumable"] == 1, "orphaned task was not made resumable")
    require(transition["to_status"] == "interrupted", "recovery transition missing")
    return {"task_id": task_id, "status": row["status"], "resumable": bool(row["resumable"]), "transition": dict(transition)}


init_db()
results = [
    scenario("file_organization", file_organization),
    scenario("markdown_batch", markdown_batch),
    scenario("safe_delete_restore", safe_delete),
    scenario("code_fix", code_fix),
    scenario("reliable_stop", reliable_stop),
    scenario("crash_recovery", crash_recovery),
]
report = {
    "schema_version": 1,
    "target_version": "12.0.0",
    "recorded_at": now_iso(),
    "runtime_directory": str(runtime),
    "summary": {"pass": sum(item["status"] == "PASS" for item in results), "fail": sum(item["status"] == "FAIL" for item in results)},
    "scenarios": results,
}
(OUTPUT / "real-scenarios.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report["summary"], ensure_ascii=False))
raise SystemExit(0 if report["summary"]["fail"] == 0 else 1)
