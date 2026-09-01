from app.database import connect, now_iso
from app.permissions import authorize, expire_task_capabilities


def request(*, task_id: str = "task-1", conversation_id: int | None = None, risk: str = "high"):
    return authorize(
        mode="ask",
        risk=risk,
        tool="write_file",
        arguments={"path": "a.txt", "content": "hello"},
        conversation_id=conversation_id,
        task_id=task_id,
    )


def approve(
    token: str,
    *,
    scope: str = "once",
    task_id: str = "task-1",
    conversation_id: int | None = None,
    arguments: dict | None = None,
    risk: str = "high",
):
    return authorize(
        mode="ask",
        risk=risk,
        tool="write_file",
        arguments=arguments or {"path": "a.txt", "content": "hello"},
        conversation_id=conversation_id,
        task_id=task_id,
        approval_tokens=[token],
        approval_scope=scope,
    )


def test_once_token_is_context_bound_and_cannot_be_replayed() -> None:
    pending = request(task_id="bound-task")
    token = pending.confirmation["approval_key"]
    assert not approve(token, task_id="other-task").allowed
    assert not approve(token, task_id="bound-task", arguments={"path": "b.txt", "content": "hello"}).allowed
    assert approve(token, task_id="bound-task").allowed
    assert not approve(token, task_id="bound-task").allowed


def test_task_scope_can_only_be_reused_in_same_task() -> None:
    pending = request(task_id="task-scope")
    token = pending.confirmation["approval_key"]
    assert approve(token, scope="task", task_id="task-scope").allowed
    assert approve(token, task_id="task-scope").allowed
    assert not approve(token, task_id="different-task").allowed


def test_session_scope_can_be_reused_across_tasks_in_same_conversation() -> None:
    with connect() as db:
        for conversation_id in (101, 202):
            db.execute(
                "INSERT OR REPLACE INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (conversation_id, "permissions", ".", "ask", now_iso(), now_iso()),
            )
    pending = request(task_id="first", conversation_id=101)
    token = pending.confirmation["approval_key"]
    assert approve(token, scope="session", task_id="first", conversation_id=101).allowed
    assert approve(token, task_id="second", conversation_id=101).allowed
    assert not approve(token, task_id="second", conversation_id=202).allowed


def test_critical_actions_are_always_once() -> None:
    pending = request(task_id="critical-task", risk="critical")
    assert pending.confirmation["allowed_scopes"] == ["once"]
    token = pending.confirmation["approval_key"]
    assert approve(token, scope="session", task_id="critical-task", risk="critical").allowed
    assert not approve(token, task_id="critical-task", risk="critical").allowed


def test_plaintext_token_is_never_persisted() -> None:
    pending = request(task_id="secret-task")
    token = pending.confirmation["approval_key"]
    with connect() as db:
        row = db.execute("SELECT token_hash FROM approval_grants ORDER BY id DESC LIMIT 1").fetchone()
    assert row["token_hash"] != token
    assert len(row["token_hash"]) == 64


def test_capability_is_bound_to_workspace_and_exact_action() -> None:
    pending = authorize(
        mode="ask",
        risk="critical",
        tool="run_command",
        arguments={
            "command": "python",
            "args": ["-m", "pytest"],
            "affected_paths": ["coverage.xml"],
        },
        task_id="capability-task",
        workspace="D:/workspace-a",
    )
    capability = pending.confirmation["capability"]
    assert capability["workspace"] == "D:/workspace-a"
    assert capability["allowed_paths"] == ["coverage.xml"]
    assert capability["allowed_commands"] == [{"command": "python", "args": ["-m", "pytest"]}]
    token = pending.confirmation["approval_key"]
    denied = authorize(
        mode="ask",
        risk="critical",
        tool="run_command",
        arguments={
            "command": "python",
            "args": ["-m", "pytest"],
            "affected_paths": ["coverage.xml"],
        },
        task_id="capability-task",
        workspace="D:/workspace-b",
        approval_tokens=[token],
    )
    assert not denied.allowed


def test_task_capabilities_expire_when_task_finishes() -> None:
    pending = request(task_id="finished-task")
    token = pending.confirmation["approval_key"]
    expire_task_capabilities("finished-task")
    assert not approve(token, task_id="finished-task").allowed


def test_readonly_mode_allows_reads_and_blocks_writes_without_approval() -> None:
    readable = authorize(
        mode="readonly",
        risk="low",
        tool="read_file",
        arguments={"path": "a.txt"},
        source="builtin",
    )
    blocked = authorize(
        mode="readonly",
        risk="medium",
        tool="write_file",
        arguments={"path": "a.txt", "content": "changed"},
        source="builtin",
    )

    assert readable.allowed is True
    assert blocked.allowed is False
    assert blocked.confirmation["status"] == "blocked"
    assert blocked.confirmation["error_code"] == "read_only_mode"
    assert "approval_key" not in blocked.confirmation
