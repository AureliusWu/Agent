"""Additive v16 file step journal; applied through backed-up database migration."""
from __future__ import annotations

import sqlite3


def migration_v46(db: sqlite3.Connection) -> None:
    existing = {row[1] for row in db.execute("PRAGMA table_info(file_transactions)")}
    for name, definition in (
        ("conversation_id", "INTEGER"),
        ("request_hash", "TEXT NOT NULL DEFAULT ''"),
        ("plan_hash", "TEXT NOT NULL DEFAULT ''"),
        ("source", "TEXT NOT NULL DEFAULT 'legacy'"),
        ("permission_mode", "TEXT NOT NULL DEFAULT ''"),
        ("journal_version", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in existing:
            db.execute(f"ALTER TABLE file_transactions ADD COLUMN {name} {definition}")
    db.execute("""CREATE TABLE IF NOT EXISTS file_transaction_steps (
        operation_id TEXT PRIMARY KEY,
        transaction_id TEXT NOT NULL,
        step_index INTEGER NOT NULL,
        operation TEXT NOT NULL,
        state TEXT NOT NULL,
        before_json TEXT NOT NULL DEFAULT '{}',
        after_json TEXT NOT NULL DEFAULT '{}',
        change_id TEXT,
        result_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(transaction_id,step_index),
        FOREIGN KEY(transaction_id) REFERENCES file_transactions(transaction_id) ON DELETE CASCADE
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_file_transactions_scope ON file_transactions(conversation_id,workspace_hash,created_at)")
