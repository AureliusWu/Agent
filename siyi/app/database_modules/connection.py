from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Any

from .migrations import memory_fingerprint, normalize_memory_content
from .schema import LONG_TERM_MEMORY_COMPATIBILITY_TRIGGERS


@contextmanager
def open_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open one transactional SQLite connection with the product defaults."""

    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.create_function("memory_normalize", 1, normalize_memory_content, deterministic=True)
    db.create_function("memory_fingerprint", 1, memory_fingerprint, deterministic=True)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")
    memory_tables = {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('memories','memory_records')"
        )
    }
    if memory_tables == {"memories", "memory_records"}:
        db.executescript(LONG_TERM_MEMORY_COMPATIBILITY_TRIGGERS)
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
    finally:
        db.close()


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Execute a read through the public facade's replaceable connection seam."""

    from app import database as facade

    with facade.connect() as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]
