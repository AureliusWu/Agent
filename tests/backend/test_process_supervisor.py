import subprocess
import sys
import uuid

from app.database import connect, init_db, now_iso
from app.process_supervisor import register_process, terminate_task_processes


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
