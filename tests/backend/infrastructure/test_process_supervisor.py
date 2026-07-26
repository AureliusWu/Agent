import subprocess
import sys
import time
import uuid

from app.database import connect, init_db, now_iso
from app.process_supervisor import (
    _process_identity,
    register_process,
    terminate_process_tree,
    terminate_task_processes,
)


def test_managed_process_is_registered_and_stopped_with_task(tmp_path) -> None:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "process", str(tmp_path), "full", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "process", stamp, stamp),
        )

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        register_process(process.pid, task_id, sys.executable, ["-c", "import time; time.sleep(30)"], process)
        assert terminate_task_processes(task_id, "cancelled") == 1
        process.wait(timeout=5)
        with connect() as db:
            record = dict(db.execute("SELECT status,stopped_at FROM managed_processes WHERE pid=?", (process.pid,)).fetchone())
        assert record["status"] == "cancelled"
        assert record["stopped_at"]
    finally:
        if process.poll() is None:
            process.kill()


def test_windows_task_stop_terminates_grandchild_process_tree(tmp_path) -> None:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "process-tree", str(tmp_path), "full", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "process-tree", stamp, stamp),
        )

    child_pid_file = tmp_path / "grandchild.pid"
    launcher = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid),encoding='ascii');"
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", launcher],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    child_pid: int | None = None
    try:
        register_process(process.pid, task_id, sys.executable, ["-c", launcher], process)
        deadline = time.monotonic() + 5
        child_pid_text = ""
        while time.monotonic() < deadline:
            if child_pid_file.exists():
                child_pid_text = child_pid_file.read_text(encoding="ascii").strip()
                if child_pid_text:
                    break
            time.sleep(0.05)
        assert child_pid_text
        child_pid = int(child_pid_text)
        assert _process_identity(child_pid) is not None

        assert terminate_task_processes(task_id, "cancelled") == 1
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _process_identity(child_pid) is not None:
            time.sleep(0.05)
        assert _process_identity(child_pid) is None
    finally:
        terminate_process_tree(process.pid)
        if child_pid is not None:
            terminate_process_tree(child_pid)
