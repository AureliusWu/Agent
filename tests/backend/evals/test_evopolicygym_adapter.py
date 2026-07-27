from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.evals.evopolicygym_adapter import (
    SiyiEvoSession,
    _require_isolated_workspace,
    run_jsonl,
)


class FakeSession:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.prompts: list[str] = []

    def start(self) -> None:
        self.started = True

    def step(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return {"text": f"handled:{prompt}", "stop": False, "data": {"status": "ok"}}

    def close(self) -> None:
        self.closed = True


def test_jsonl_adapter_keeps_one_session_across_turns() -> None:
    session = FakeSession()
    stdin = io.StringIO(
        '{"type":"prompt","turn":0,"message":"first"}\n'
        '{"type":"prompt","turn":1,"message":"continue"}\n'
    )
    stdout = io.StringIO()
    assert run_jsonl(session, stdin, stdout, io.StringIO()) == 0
    assert session.started and session.closed
    assert session.prompts == ["first", "continue"]
    rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [row["turn"] for row in rows] == [0, 1]
    assert all(row["data"]["status"] == "ok" for row in rows)


def test_jsonl_adapter_fails_closed_on_invalid_protocol() -> None:
    session = FakeSession()
    stdout = io.StringIO()
    assert run_jsonl(session, io.StringIO('{"type":"other","turn":0}\n'), stdout, io.StringIO()) == 0
    row = json.loads(stdout.getvalue())
    assert row["stop"] is True
    assert row["data"]["error_type"] == "ValueError"
    assert session.prompts == []


def test_protocol_frames_are_ascii_safe_on_windows_code_pages() -> None:
    session = FakeSession()
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="gbk")
    assert (
        run_jsonl(
            session,
            io.StringIO('{"type":"prompt","turn":0,"message":"📋"}\n'),
            stdout,
            io.StringIO(),
        )
        == 0
    )
    stdout.flush()
    wire = raw.getvalue().decode("ascii")
    assert json.loads(wire)["text"] == "handled:📋"


def test_isolated_bridge_fetches_loopback_context_and_submits_real_policy(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "system").mkdir(parents=True)
    submissions: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "state": {"is_finalized": False, "remaining_budget": 8},
                    "max_episodes_per_submit": 8,
                    "env_meta": {"n_env_instances": 8},
                },
            )
        if request.url.path == "/task":
            return httpx.Response(
                200,
                text="# Toy\nCreate system/policy.py",
                headers={"content-type": "text/plain; charset=utf-8"},
            )
        if request.url.path == "/submit":
            submissions.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "accepted"})
        raise AssertionError(request.url)

    session = SiyiEvoSession(
        repo_root=tmp_path,
        workspace=workspace,
        data_root=tmp_path / "data",
        model_api_key="configured",
    )
    session.client = httpx.Client(transport=httpx.MockTransport(handler))
    prompt = session._augment_with_benchmark_context(
        "GET http://127.0.0.1:4321/info then continue"
    )
    assert "OFFICIAL_TASK:\n# Toy" in prompt
    assert session._materialize_policy_from_response(
        "POLICY_PY_BEGIN\nclass Policy:\n    pass\nPOLICY_PY_END"
    )
    assert (workspace / "system" / "policy.py").read_text(
        encoding="utf-8"
    ) == "class Policy:\n    pass\n"
    assert session._submit_policy_if_ready() == {"status": "accepted"}
    assert submissions == [{"env_instances": list(range(8))}]
    session.client.close()


def test_bridge_materializes_only_pending_policy_proposal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "system").mkdir(parents=True)
    data_root = tmp_path / "runtime"
    (data_root / "data").mkdir(parents=True)
    database = data_root / "data" / "agent.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE task_operations("
            "task_id TEXT, tool TEXT, status TEXT, result TEXT, started_at TEXT)"
        )
        db.execute(
            "INSERT INTO task_operations VALUES(?,?,?,?,?)",
            (
                "task-1",
                "create_file",
                "waiting_confirmation",
                json.dumps(
                    {
                        "arguments": {
                            "path": "system/policy.py",
                            "content": "class Policy:\n    pass\n",
                        }
                    }
                ),
                "2026-07-23T00:00:00Z",
            ),
        )
    session = SiyiEvoSession(
        repo_root=tmp_path,
        workspace=workspace,
        data_root=data_root,
        model_api_key="configured",
    )
    assert session._materialize_pending_policy("task-1")
    assert (workspace / "system" / "policy.py").read_text(
        encoding="utf-8"
    ) == "class Policy:\n    pass\n"
    assert not session._materialize_pending_policy("different-task")


def test_evo_workspace_layout_must_be_isolated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    system = workspace / "system"
    feedback = workspace / "feedback"
    system.mkdir(parents=True)
    feedback.mkdir()
    (workspace / "AGENTS.md").write_text("rules", encoding="utf-8")
    _require_isolated_workspace(workspace, system, feedback)
    with pytest.raises(RuntimeError):
        _require_isolated_workspace(workspace, tmp_path / "system", feedback)
