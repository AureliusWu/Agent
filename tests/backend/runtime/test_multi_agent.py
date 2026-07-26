import asyncio
import json
import uuid
from pathlib import Path

from app.database import connect, init_db, now_iso
from app.runtime.multi_agent import (
    ChildAgentSpec,
    READ_ONLY_CHILD_TOOLS,
    _run_child,
    cancel_child_agents,
    ensure_root_agent,
    run_independent_verifier,
    run_orchestration_prelude,
    task_agent_trace,
)
from app.cognition.planning import build_task_plan


def _parent(workspace: Path, mode: str = "planner_executor") -> tuple[int, str]:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "multi", str(workspace), "full", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, orchestration_mode, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "multi", mode, stamp, stamp),
        )
    ensure_root_agent(
        task_id,
        mode,
        objective="test",
        token_budget=10_000,
        tool_allowlist=READ_ONLY_CHILD_TOOLS,
        file_scope=("**",),
        timeout_seconds=30,
    )
    return conversation_id, task_id


def test_planner_executor_records_bounded_child_context(tmp_path: Path) -> None:
    conversation_id, task_id = _parent(tmp_path)
    plan = build_task_plan(task_id, "分析当前工作区，不修改文件", READ_ONLY_CHILD_TOOLS)

    async def completion(messages, api_key=None, **kwargs):
        return {
            "role": "assistant",
            "content": "1. 读取入口。2. 核对风险。3. 运行已有检查。",
            "_metrics": {"usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}},
        }

    result = asyncio.run(run_orchestration_prelude(
        task_id=task_id,
        mode="planner_executor",
        agent_count=1,
        prompt="分析当前工作区，不修改文件",
        plan=plan,
        conversation_id=conversation_id,
        workspace=str(tmp_path),
        api_key=None,
        completion_fn=completion,
    ))

    assert len(result.children) == 1
    assert result.children[0].role == "planner"
    assert result.usage["total_tokens"] == 18
    assert "untrusted-content" in result.context
    trace = task_agent_trace(task_id)
    assert [agent["depth"] for agent in trace["agents"]] == [0, 1]
    assert trace["agents"][1]["tool_allowlist"]


def test_parallel_explorers_run_concurrently_with_unique_roles(tmp_path: Path) -> None:
    conversation_id, task_id = _parent(tmp_path, "parallel_explorers")
    plan = build_task_plan(task_id, "分析当前工作区", READ_ONLY_CHILD_TOOLS)
    active = 0
    maximum = 0

    async def completion(messages, api_key=None, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.03)
            return {"role": "assistant", "content": "独立分析结果", "_metrics": {"usage": {"total_tokens": 20}}}
        finally:
            active -= 1

    result = asyncio.run(run_orchestration_prelude(
        task_id=task_id,
        mode="parallel_explorers",
        agent_count=3,
        prompt="分析当前工作区",
        plan=plan,
        conversation_id=conversation_id,
        workspace=str(tmp_path),
        api_key=None,
        completion_fn=completion,
    ))

    assert maximum >= 2
    assert {item.role for item in result.children} == {"architecture_explorer", "risk_explorer", "test_explorer"}
    assert result.usage["total_tokens"] == 60


def test_child_agent_cannot_use_write_tool(tmp_path: Path) -> None:
    conversation_id, task_id = _parent(tmp_path)
    calls = 0

    async def completion(messages, api_key=None, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "forbidden-write",
                "type": "function",
                "function": {"name": "write_file", "arguments": '{"path":"owned.txt","content":"unsafe"}'},
            }]}
        tool_result = json.loads(next(item["content"] for item in messages if item.get("role") == "tool"))
        assert tool_result["error_code"] == "child_capability_denied"
        return {"role": "assistant", "content": "已停止越权操作"}

    spec = ChildAgentSpec(
        uuid.uuid4().hex,
        task_id,
        f"{task_id}:root",
        "planner",
        "planner_executor",
        "只读分析",
        "分析结果",
        2_000,
        READ_ONLY_CHILD_TOOLS,
        ("**",),
        10,
    )
    result = asyncio.run(_run_child(spec, conversation_id=conversation_id, workspace=str(tmp_path), api_key=None, completion_fn=completion))

    assert result.status == "completed"
    assert not (tmp_path / "owned.txt").exists()
    assert any(event["event_type"] == "tool_blocked" for event in task_agent_trace(task_id)["events"])


def test_child_agent_file_scope_is_enforced(tmp_path: Path) -> None:
    (tmp_path / "allowed.txt").write_text("allowed", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    conversation_id, task_id = _parent(tmp_path)
    calls = 0

    async def completion(messages, api_key=None, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "out-of-scope",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"secret.txt"}'},
            }]}
        return {"role": "assistant", "content": "范围外文件未读取"}

    spec = ChildAgentSpec(
        uuid.uuid4().hex,
        task_id,
        f"{task_id}:root",
        "planner",
        "planner_executor",
        "读取 allowed.txt",
        "分析结果",
        2_000,
        ("read_file",),
        ("allowed.txt",),
        10,
    )
    asyncio.run(_run_child(spec, conversation_id=conversation_id, workspace=str(tmp_path), api_key=None, completion_fn=completion))
    blocked = [event for event in task_agent_trace(task_id)["events"] if event["event_type"] == "tool_blocked"]
    assert blocked and "超出" in blocked[0]["details"]["reason"]


def test_child_token_budget_is_hard_limit(tmp_path: Path) -> None:
    conversation_id, task_id = _parent(tmp_path)

    async def completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "too expensive", "_metrics": {"usage": {"total_tokens": 600}}}

    spec = ChildAgentSpec(
        uuid.uuid4().hex,
        task_id,
        f"{task_id}:root",
        "planner",
        "planner_executor",
        "分析",
        "结果",
        500,
        (),
        ("**",),
        10,
    )
    result = asyncio.run(_run_child(spec, conversation_id=conversation_id, workspace=str(tmp_path), api_key=None, completion_fn=completion))
    assert result.status == "failed"
    assert result.total_tokens == 600
    with connect() as db:
        stored = dict(db.execute("SELECT status, error FROM agent_runs WHERE id=?", (spec.id,)).fetchone())
    assert stored["status"] == "failed"
    assert "预算" in stored["error"]


def test_generator_verifier_can_request_revision(tmp_path: Path) -> None:
    conversation_id, task_id = _parent(tmp_path, "generator_verifier")
    plan = build_task_plan(task_id, "检查 result.txt", READ_ONLY_CHILD_TOOLS)

    async def completion(messages, api_key=None, **kwargs):
        return {
            "role": "assistant",
            "content": '{"verdict":"revise","summary":"缺少真实测试","issues":["运行现有测试"]}',
            "_metrics": {"usage": {"total_tokens": 22}},
        }

    verdict, result = asyncio.run(run_independent_verifier(
        task_id=task_id,
        prompt="检查 result.txt",
        candidate="已完成",
        plan=plan,
        conversation_id=conversation_id,
        workspace=str(tmp_path),
        api_key=None,
        completion_fn=completion,
    ))
    assert result.role == "verifier"
    assert verdict["verdict"] == "revise"
    assert verdict["issues"] == ["运行现有测试"]


def test_parent_cancellation_marks_running_children(tmp_path: Path) -> None:
    _, task_id = _parent(tmp_path)
    child_id = uuid.uuid4().hex
    with connect() as db:
        db.execute(
            "INSERT INTO agent_runs(id, parent_task_id, parent_agent_id, role, orchestration_mode, status, objective, expected_output, token_budget, tool_allowlist, file_scope, timeout_seconds, risk_level, depth, started_at) "
            "VALUES(?,?,?,?,?,'running',?,?,?,?,?,?,?,?,?)",
            (child_id, task_id, f"{task_id}:root", "planner", "planner_executor", "plan", "result", 1000, "[]", "[]", 10, "low", 1, now_iso()),
        )
    cancel_child_agents(task_id)
    with connect() as db:
        child = dict(db.execute("SELECT status, error FROM agent_runs WHERE id=?", (child_id,)).fetchone())
    assert child == {"status": "cancelled", "error": "parent_cancelled"}
