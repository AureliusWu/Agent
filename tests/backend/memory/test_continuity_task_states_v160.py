"""Continuity must use the runtime's exhaustive task-state classification."""

import sqlite3

import pytest

from app.memory import consolidation
from app.runtime.task_state import NONTERMINAL_TASK_STATUSES, TaskStatus


@pytest.mark.parametrize("status", list(TaskStatus))
def test_continuity_classifies_every_runtime_status(status: TaskStatus, tmp_path, monkeypatch) -> None:
    # Execute the production SELECT against an isolated table so prior tests or
    # the LIMIT 20 cannot hide an incorrectly classified status.
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE agent_tasks(id TEXT, prompt TEXT, status TEXT, current_step TEXT, updated_at TEXT)")
        db.execute("INSERT INTO agent_tasks VALUES(?,?,?,?,?)", ("state-test", "test", status.value, status.value, "2026-09-30"))
        monkeypatch.setattr(consolidation, "rows", lambda query, params=(): [dict(row) for row in db.execute(query, params).fetchall()])
        monkeypatch.setattr(consolidation, "_autobiography_path", lambda: tmp_path / "continuity.md")
        result = consolidation.build_continuity_snapshot()
    assert ("state-test" in result["task_ids"]) == (status in NONTERMINAL_TASK_STATUSES)
