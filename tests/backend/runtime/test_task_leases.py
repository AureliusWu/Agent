import time
import uuid

import pytest

from app.database import connect, init_db, now_iso
from app.runtime.task_leases import TaskLeaseConflict, acquire_task_lease, release_task_lease, renew_task_lease


def _running_task() -> str:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "lease", "", "ask", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "lease test", stamp, stamp),
        )
    return task_id


def test_task_lease_is_exclusive_and_owner_can_renew() -> None:
    task_id = _running_task()
    lease = acquire_task_lease(task_id, ttl_seconds=20)
    with pytest.raises(TaskLeaseConflict):
        acquire_task_lease(task_id, ttl_seconds=20)

    renewed = renew_task_lease(lease, ttl_seconds=40)
    assert renewed.expires_at > lease.expires_at
    assert release_task_lease(renewed) is True
    replacement = acquire_task_lease(task_id, ttl_seconds=20)
    assert replacement.generation == lease.generation + 1
    assert release_task_lease(replacement) is True


def test_init_db_preserves_running_task_with_live_lease() -> None:
    task_id = _running_task()
    lease = acquire_task_lease(task_id, ttl_seconds=20)
    init_db()
    with connect() as db:
        status = db.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0]
    assert status == "running"
    release_task_lease(lease)


def test_init_db_interrupts_task_owned_by_dead_process() -> None:
    task_id = _running_task()
    lease = acquire_task_lease(task_id, ttl_seconds=20)
    with connect() as db:
        db.execute(
            "UPDATE task_leases SET owner_pid=?,expires_at=? WHERE task_id=?",
            (2_147_483_647, time.time() + 60, task_id),
        )
    init_db()
    with connect() as db:
        task_status = db.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()[0]
        lease_status = db.execute("SELECT status FROM task_leases WHERE task_id=?", (task_id,)).fetchone()[0]
    assert task_status == "interrupted"
    assert lease_status == "expired"
    assert release_task_lease(lease) is False


def test_expired_task_lease_is_taken_over_with_new_generation() -> None:
    task_id = _running_task()
    lease = acquire_task_lease(task_id, ttl_seconds=20)
    with connect() as db:
        db.execute(
            "UPDATE task_leases SET expires_at=? WHERE task_id=?",
            (time.time() - 1, task_id),
        )
    replacement = acquire_task_lease(task_id, ttl_seconds=20)
    assert replacement.generation == lease.generation + 1
    assert replacement.owner_instance_id == lease.owner_instance_id
    assert release_task_lease(replacement) is True
