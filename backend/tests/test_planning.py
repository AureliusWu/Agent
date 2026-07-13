import uuid
from pathlib import Path

from app.database import connect, now_iso
from app.planning import build_task_plan, load_task_plan, save_task_plan
from app.tool_registry import REGISTRY


def _task(tmp_path: Path) -> str:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "planner", str(tmp_path), "agent", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "plan", stamp, stamp),
        )
    return task_id


def test_planner_creates_dependencies_risks_and_acceptance_criteria(tmp_path: Path) -> None:
    task_id = _task(tmp_path)
    plan = build_task_plan(task_id, "修复 main.py 并运行测试，不要修改其他文件。", REGISTRY)
    assert [step.id for step in plan.steps] == ["inspect", "execute", "verify", "finalize"]
    assert plan.steps[2].risk == "critical"
    assert plan.strict_scope is True
    assert {item.kind for item in plan.acceptance_criteria} >= {"changes_recorded", "path_state", "verification_command", "scope_control"}
    save_task_plan(plan)
    loaded = load_task_plan(task_id)
    assert loaded == plan


def test_planner_understands_no_write_and_unavailable_hardware() -> None:
    read_only = build_task_plan("read", "说明项目结构和测试位置，不要修改文件。", REGISTRY)
    assert read_only.task_kind == "workspace_analysis"
    assert "changes_recorded" not in {item.id for item in read_only.acceptance_criteria}

    blocked = build_task_plan("blocked", "读取当前未连接的专用硬件温度并写入 result.json；没有接口时阻塞。", REGISTRY)
    assert blocked.task_kind == "blocked"
    assert blocked.blocked_reason
    assert "blocked_safely" in {item.id for item in blocked.acceptance_criteria}


def test_planner_allows_hardware_task_when_matching_tool_is_available() -> None:
    prompt = "读取当前未连接的传感器温度并写入 result.json，没有接口时阻塞。"
    plan = build_task_plan(
        "hardware-ready",
        prompt,
        {"mcp__sensor__temperature"},
    )
    assert plan.task_kind != "blocked"
    assert plan.blocked_reason is None

    mismatched = build_task_plan("hardware-mismatch", prompt, {"mcp__camera__capture"})
    assert mismatched.task_kind == "blocked"
    assert mismatched.blocked_reason


def test_blocked_plan_does_not_require_unavailable_verification() -> None:
    plan = build_task_plan(
        "blocked-verify",
        "读取当前未连接的传感器温度并运行测试，没有接口时阻塞。",
        REGISTRY,
    )
    assert plan.task_kind == "blocked"
    assert "verification_command" not in {item.id for item in plan.acceptance_criteria}


def test_move_plan_expects_source_removed_and_destination_present() -> None:
    plan = build_task_plan(
        "move-file",
        "Move notes/draft.txt to archive/draft.txt.",
        REGISTRY,
    )

    path_states = [item for item in plan.acceptance_criteria if item.kind == "path_state"]
    assert [(item.parameters["path"], item.parameters["exists"]) for item in path_states] == [
        ("notes/draft.txt", False),
        ("archive/draft.txt", True),
    ]
