"""Lease timing and same-transaction write fences; no live providers or data."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import settings
from app.database import connect, init_db, now_iso
from app.runtime import task_leases as leases


@pytest.fixture(autouse=True)
def owned_database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "lease-boundaries.db")
    monkeypatch.setattr(settings, "log_path", tmp_path / "lease-boundaries.log")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "speech_enabled", False)
    monkeypatch.setenv("AGENT_DATA_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("AGENT_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "absent-provider.json"))
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "mock")
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "false")
    token = leases._ACTIVE_TASK_LEASE.set(None)
    init_db()
    try:
        yield tmp_path
    finally:
        leases.reset_task_lease(token)


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(leases.time, "time", lambda: value[0])
    return value


def running_task() -> tuple[int, str]:
    stamp, task_id = now_iso(), uuid.uuid4().hex
    with connect() as db:
        conversation_id = int(db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("lease boundary", "", "ask", stamp, stamp),
        ).lastrowid)
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "boundary", stamp, stamp),
        )
    return conversation_id, task_id


class DelayedConnection:
    """Delegate real SQLite state, injecting clock movement rather than sleep."""
    def __init__(self, db, clock, point, amount):
        self.db, self.clock, self.point, self.amount = db, clock, point, amount
        self.delayed = False

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, parameters=()):
        statement = sql.strip().upper()
        write_lock = statement == "BEGIN IMMEDIATE" or (
            statement.startswith("UPDATE TASK_LEASES") and not self.db.in_transaction
        )
        if self.point == "write_lock" and write_lock and not self.delayed:
            self.clock[0] += self.amount
            self.delayed = True
        cursor = self.db.execute(sql, parameters)
        if self.point == "read" and statement.startswith("SELECT") and "FROM TASK_LEASES" in statement:
            outer = self

            class DelayedCursor:
                def fetchone(self):
                    row = cursor.fetchone()
                    outer.clock[0] += outer.amount
                    return row

            return DelayedCursor()
        return cursor


def delay_connection(monkeypatch, clock, point, amount):
    original = leases.connect

    @contextmanager
    def delayed():
        with original() as db:
            if point == "connect":
                clock[0] += amount
            yield DelayedConnection(db, clock, point, amount)

    monkeypatch.setattr(leases, "connect", delayed)


@pytest.mark.parametrize("point", ["connect", "write_lock"])
def test_acquire_samples_ttl_after_connection_and_write_lock(clock, monkeypatch, point):
    _, task_id = running_task()
    delay_connection(monkeypatch, clock, point, 31)
    lease = leases.acquire_task_lease(task_id)
    actual_expiry = lease.expires_at
    assert settings.task_lease_seconds == 30
    assert actual_expiry == 1061
    with connect() as db:
        assert db.execute("SELECT expires_at FROM task_leases WHERE task_id=?", (task_id,)).fetchone()[0] == 1061


@pytest.mark.parametrize("point", ["connect", "write_lock"])
def test_renew_samples_ttl_after_connection_and_write_lock(clock, monkeypatch, point):
    _, task_id = running_task()
    lease = leases.acquire_task_lease(task_id)
    delay_connection(monkeypatch, clock, point, 31)
    renewed = leases.renew_task_lease(lease)
    actual_expiry = renewed.expires_at
    assert actual_expiry == 1061
    assert renewed.generation == lease.generation


def test_current_uses_time_after_connection_enter(clock, monkeypatch):
    _, task_id = running_task()
    lease = leases.acquire_task_lease(task_id)
    with connect() as db:
        db.execute("UPDATE task_leases SET expires_at=1005 WHERE task_id=?", (task_id,))
    delay_connection(monkeypatch, clock, "connect", 6)
    assert leases.task_lease_is_current(lease) is False


def test_current_uses_time_after_supplied_connection_read(clock):
    _, task_id = running_task()
    lease = leases.acquire_task_lease(task_id)
    with connect() as db:
        db.execute("UPDATE task_leases SET expires_at=1005 WHERE task_id=?", (task_id,))
    with connect() as db:
        delayed = DelayedConnection(db, clock, "read", 6)
        assert leases.task_lease_is_current(lease, db=delayed) is False


def test_expiry_is_strict_and_current_uses_database_not_old_object(clock):
    _, task_id = running_task()
    lease = leases.acquire_task_lease(task_id)
    clock[0] = lease.expires_at
    assert leases.task_lease_is_current(lease) is False
    leases.renew_task_lease(lease)
    assert leases.task_lease_is_current(lease) is True


def test_same_owner_time_expired_active_lease_still_renews(clock):
    _, task_id = running_task()
    lease = leases.acquire_task_lease(task_id)
    clock[0] = 1031
    assert leases.task_lease_is_current(lease) is False
    renewed = leases.renew_task_lease(lease)
    assert renewed.generation == lease.generation
    assert leases.task_lease_is_current(renewed) is True


@pytest.mark.parametrize("mismatch", ["owner", "token", "generation", "released", "expired"])
def test_identity_and_status_mismatches_cannot_renew_or_pass_fence(clock, mismatch):
    _, task_id = running_task()
    lease = leases.acquire_task_lease(task_id)
    tested = lease
    if mismatch == "owner":
        tested = replace(lease, owner_instance_id="other-owner")
    elif mismatch == "token":
        tested = replace(lease, token="synthetic-wrong-token")
    elif mismatch == "generation":
        tested = replace(lease, generation=lease.generation + 1)
    else:
        with connect() as db:
            db.execute("UPDATE task_leases SET status=? WHERE task_id=?", (mismatch, task_id))
    assert leases.task_lease_is_current(tested) is False
    with pytest.raises(leases.TaskLeaseConflict):
        leases.renew_task_lease(tested)
    token = leases.bind_task_lease(tested)
    try:
        with pytest.raises(leases.TaskLeaseConflict), connect() as db:
            leases.fence_current_task_write(task_id, db=db)
    finally:
        leases.reset_task_lease(token)


def test_live_owner_is_not_displaced_after_acquire_wait(clock, monkeypatch):
    _, task_id = running_task()
    first = leases.acquire_task_lease(task_id, ttl_seconds=60)
    delay_connection(monkeypatch, clock, "write_lock", 31)
    with pytest.raises(leases.TaskLeaseConflict):
        leases.acquire_task_lease(task_id)
    with connect() as db:
        assert db.execute("SELECT generation FROM task_leases WHERE task_id=?", (task_id,)).fetchone()[0] == first.generation


def test_unbound_and_wrong_task_fences_do_not_start_transaction(clock):
    _, task_id = running_task()
    with connect() as db:
        assert leases.fence_current_task_write(task_id, db=db) is None
        assert db.in_transaction is False
    token = leases.bind_task_lease(leases.acquire_task_lease(task_id))
    try:
        with connect() as db:
            with pytest.raises(leases.TaskLeaseConflict):
                leases.fence_current_task_write("different-task", db=db)
            assert db.in_transaction is False
    finally:
        leases.reset_task_lease(token)


@pytest.mark.parametrize("transaction", ["fresh", "autocommit", "read", "write"])
def test_fence_blocks_real_second_connection_takeover_until_write_commit(clock, monkeypatch, transaction):
    conversation_id, task_id = running_task()
    first = leases.acquire_task_lease(task_id)
    token = leases.bind_task_lease(first)
    writer_started, writer_finished = threading.Event(), threading.Event()
    original = leases.connect

    @contextmanager
    def observed_connect():
        with original() as db:
            class ObservedConnection:
                def __getattr__(self, name):
                    return getattr(db, name)

                def execute(self, sql, parameters=()):
                    if sql.strip().upper() == "BEGIN IMMEDIATE":
                        writer_started.set()
                    return db.execute(sql, parameters)

            yield ObservedConnection()

    def compete():
        try:
            return leases.acquire_task_lease(task_id)
        finally:
            writer_finished.set()

    db = sqlite3.connect(settings.database_path, isolation_level=None if transaction == "autocommit" else "")
    db.row_factory = sqlite3.Row
    replacement = None
    try:
        if transaction == "read":
            db.execute("BEGIN DEFERRED")
            db.execute("SELECT generation FROM task_leases WHERE task_id=?", (task_id,)).fetchone()
        elif transaction == "write":
            db.execute("BEGIN IMMEDIATE")
        leases.fence_current_task_write(task_id, db=db)
        clock[0] = 1031
        monkeypatch.setattr(leases, "connect", observed_connect)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(compete)
            try:
                assert writer_started.wait(3), "competing writer never attempted BEGIN"
                assert not writer_finished.wait(0.2), "takeover committed between fence and owned write"
                db.execute(
                    "INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
                    (conversation_id, "assistant", "owned write", task_id, now_iso()),
                )
                db.commit()
            finally:
                db.rollback()
                replacement = future.result(timeout=5)
        assert replacement.generation == first.generation + 1
        with connect() as check:
            assert check.execute("SELECT COUNT(*) FROM messages WHERE task_id=?", (task_id,)).fetchone()[0] == 1
    finally:
        db.rollback()
        db.close()
        leases.reset_task_lease(token)
        if replacement is not None:
            leases.release_task_lease(replacement)


def test_existing_stale_read_snapshot_upgrade_fails_closed_without_committing(clock):
    conversation_id, task_id = running_task()
    first = leases.acquire_task_lease(task_id)
    token = leases.bind_task_lease(first)
    replacement = None
    try:
        with connect() as db:
            db.execute("BEGIN DEFERRED")
            db.execute("SELECT generation FROM task_leases WHERE task_id=?", (task_id,)).fetchone()
            with connect() as other:
                other.execute("UPDATE task_leases SET expires_at=999 WHERE task_id=?", (task_id,))
            replacement = leases.acquire_task_lease(task_id)
            with pytest.raises(sqlite3.OperationalError) as error:
                leases.fence_current_task_write(task_id, db=db)
            assert error.value.sqlite_errorcode in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT}
            assert db.in_transaction is True
            db.rollback()
        with connect() as check:
            assert check.execute("SELECT generation FROM task_leases WHERE task_id=?", (task_id,)).fetchone()[0] == replacement.generation
            assert check.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0] == 0
    finally:
        leases.reset_task_lease(token)
        if replacement is not None:
            leases.release_task_lease(replacement)


@pytest.mark.parametrize("already_writing", [False, True])
def test_owned_fence_and_following_writes_roll_back_together(clock, already_writing):
    conversation_id, task_id = running_task()
    lease = leases.acquire_task_lease(task_id)
    token = leases.bind_task_lease(lease)
    try:
        with pytest.raises(RuntimeError, match="rollback witness"), connect() as db:
            if already_writing:
                db.execute("BEGIN IMMEDIATE")
            leases.fence_current_task_write(task_id, db=db)
            db.execute(
                "INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",
                (conversation_id, "assistant", "rollback", task_id, now_iso()),
            )
            raise RuntimeError("rollback witness")
        with connect() as check:
            assert check.execute("SELECT COUNT(*) FROM messages WHERE task_id=?", (task_id,)).fetchone()[0] == 0
            assert check.execute("SELECT generation FROM task_leases WHERE task_id=?", (task_id,)).fetchone()[0] == lease.generation
    finally:
        leases.reset_task_lease(token)
