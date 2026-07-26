from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.config import settings
from app.database import connect, now_iso


RUNTIME_INSTANCE_ID = uuid.uuid4().hex


class TaskLeaseConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    owner_instance_id: str
    token: str
    generation: int
    expires_at: float


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def acquire_task_lease(task_id: str, *, ttl_seconds: int | None = None) -> TaskLease:
    ttl = int(ttl_seconds or settings.task_lease_seconds)
    stamp = now_iso()
    expires_at = time.time() + ttl
    token = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM task_leases WHERE task_id=?", (task_id,)).fetchone()
        if current and current["status"] == "active" and float(current["expires_at"]) > time.time():
            raise TaskLeaseConflict(f"task {task_id} is owned by another runtime instance")
        generation = int(current["generation"] if current else 0) + 1
        db.execute(
            "INSERT INTO task_leases(task_id,owner_instance_id,owner_pid,token_hash,generation,acquired_at,heartbeat_at,expires_at,released_at,status) "
            "VALUES(?,?,?,?,?,?,?,?,NULL,'active') ON CONFLICT(task_id) DO UPDATE SET "
            "owner_instance_id=excluded.owner_instance_id,owner_pid=excluded.owner_pid,token_hash=excluded.token_hash,"
            "generation=excluded.generation,acquired_at=excluded.acquired_at,heartbeat_at=excluded.heartbeat_at,"
            "expires_at=excluded.expires_at,released_at=NULL,status='active'",
            (task_id, RUNTIME_INSTANCE_ID, os.getpid(), _token_hash(token), generation, stamp, stamp, expires_at),
        )
    return TaskLease(task_id, RUNTIME_INSTANCE_ID, token, generation, expires_at)


def renew_task_lease(lease: TaskLease, *, ttl_seconds: int | None = None) -> TaskLease:
    ttl = int(ttl_seconds or settings.task_lease_seconds)
    expires_at = time.time() + ttl
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "UPDATE task_leases SET heartbeat_at=?,expires_at=? WHERE task_id=? AND owner_instance_id=? "
            "AND token_hash=? AND generation=? AND status='active'",
            (stamp, expires_at, lease.task_id, lease.owner_instance_id, _token_hash(lease.token), lease.generation),
        )
        if cursor.rowcount != 1:
            raise TaskLeaseConflict(f"task {lease.task_id} lease was lost")
    return TaskLease(lease.task_id, lease.owner_instance_id, lease.token, lease.generation, expires_at)


def release_task_lease(lease: TaskLease, *, status: str = "released") -> bool:
    with connect() as db:
        cursor = db.execute(
            "UPDATE task_leases SET status=?,released_at=?,expires_at=? WHERE task_id=? AND owner_instance_id=? "
            "AND token_hash=? AND generation=? AND status='active'",
            (status, now_iso(), time.time(), lease.task_id, lease.owner_instance_id, _token_hash(lease.token), lease.generation),
        )
    return cursor.rowcount == 1


async def maintain_task_lease(
    lease: TaskLease,
    *,
    on_lost: Callable[[TaskLeaseConflict], Awaitable[None] | None],
) -> None:
    current = lease
    while True:
        await asyncio.sleep(settings.task_heartbeat_seconds)
        try:
            current = renew_task_lease(current)
        except TaskLeaseConflict as exc:
            result = on_lost(exc)
            if result is not None:
                await result
            return
