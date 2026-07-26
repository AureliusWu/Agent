import asyncio
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from app.database import connect, now_iso
from app.cognition.planning import PlanValidationError, build_task_plan, load_task_plan, save_task_plan, validate_task_contract
from app.cognition.semantic_planner import PlannerContext, build_semantic_task_plan
from app.tools.registry import REGISTRY


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

    plan_only = build_task_plan("plan-only", "检查 API 超时处理并给出最小修改计划，不要修改代码。", REGISTRY)
    assert plan_only.task_kind == "workspace_analysis"
    assert "workspace.write" not in plan_only.required_capabilities

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


def test_move_plan_excludes_unrelated_read_path_from_change_contract() -> None:
    plan = build_task_plan(
        "move-with-read",
        "读取 input.txt；创建 summary.txt，再把 summary.txt 移动到 archive/result.txt，最后读取移动后的文件。",
        REGISTRY,
    )

    path_states = [item for item in plan.acceptance_criteria if item.kind == "path_state"]
    assert [(item.parameters["path"], item.parameters["exists"]) for item in path_states] == [
        ("summary.txt", False),
        ("archive/result.txt", True),
    ]
    assert plan.expected_paths == ("input.txt", "summary.txt", "archive/result.txt")
    assert plan.expected_changes == ("summary.txt", "archive/result.txt")


def test_reading_code_while_writing_report_does_not_require_code_verification() -> None:
    plan = build_task_plan(
        "read-code-write-report",
        "读取 siyi/app/database.py；创建 build/acceptance/proof.txt 并写入结果。不要修改其他文件。",
        REGISTRY,
    )

    assert plan.expected_paths == ("siyi/app/database.py", "build/acceptance/proof.txt")
    assert plan.expected_changes == ("build/acceptance/proof.txt",)
    assert "command.execute" not in plan.required_capabilities
    assert "verify_code" not in {criterion.id for criterion in plan.acceptance_criteria}


def test_planner_uses_only_profile_authorized_move_tools() -> None:
    allowed = {"list_files", "read_file", "create_directory", "move_file", "rename_file"}
    plan = build_task_plan("file-profile", "创建 archive 目录，然后把 draft.txt 移动到 archive/draft.txt。", allowed)

    validate_task_contract(replace(plan, budget_limit=100), allowed)
    execute = next(step for step in plan.steps if step.id == "execute")
    assert execute.tools == ("create_directory", "move_file", "rename_file")
    assert "write_file" not in {tool for step in plan.steps for tool in step.tools}


def test_semantic_planner_is_bounded_by_policy_guard() -> None:
    async def fake_planner(messages, api_key=None, **kwargs):
        return {
            "role": "assistant",
            "content": """{
                "goal": "扩大到整台电脑",
                "assumptions": ["项目使用 pytest"],
                "constraints": ["保持兼容"],
                "steps": [{"id":"inspect_semantics","description":"检查目标模块","tools":["read_file","delete_everything"],"risk":"low"}],
                "expected_changes": ["siyi/app/main.py", "../../outside.txt", "C:/Windows/win.ini"],
                "forbidden_changes": ["不要改配置"],
                "acceptance_criteria": ["目标测试通过"],
                "verification_commands": ["pytest tests/siyi/test_api.py"],
                "required_capabilities": ["whole_computer.full"],
                "preferred_executor": "unrestricted_cloud",
                "risk": "low",
                "requires_user_input": false
            }""",
            "_metrics": {"usage": {"prompt_tokens": 10, "completion_tokens": 20}},
        }

    result = asyncio.run(
        build_semantic_task_plan(
            "semantic",
            "修复 siyi/app/main.py 并运行测试，只能访问当前工作区。",
            REGISTRY,
            complete=fake_planner,
            api_key="test-key",
            context=PlannerContext(budget_limit=2000, preferred_model="deepseek-chat", privacy_scope="private"),
        )
    )

    plan = result.plan
    assert plan.goal.startswith("修复 siyi/app/main.py")
    assert plan.preferred_executor == "local_windows"
    assert plan.required_capabilities == ("workspace.read", "workspace.write", "command.execute")
    assert plan.expected_changes == ("siyi/app/main.py",)
    assert "delete_everything" not in {tool for step in plan.steps for tool in step.tools}
    assert {step.id for step in plan.steps} >= {"inspect_semantics", "inspect", "execute", "verify", "finalize"}
    assert plan.risk == "medium"
    assert plan.planner_source == "semantic_model"
    assert plan.privacy_scope == "private"
    assert result.metrics["usage"]["completion_tokens"] == 20


def test_semantic_planner_falls_back_to_deterministic_contract() -> None:
    async def invalid_planner(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "not-json"}

    result = asyncio.run(
        build_semantic_task_plan(
            "fallback",
            "分析项目结构，不要修改文件。",
            REGISTRY,
            complete=invalid_planner,
            api_key="test-key",
            context=PlannerContext(budget_limit=500),
        )
    )
    assert result.plan.planner_source == "deterministic_fallback"
    assert result.plan.task_kind == "workspace_analysis"
    assert result.fallback_reason and "ValueError" in result.fallback_reason


def test_contract_validator_rejects_executor_or_workspace_expansion() -> None:
    plan = replace(build_task_plan("guard", "分析项目结构", REGISTRY), budget_limit=100)
    with pytest.raises(PlanValidationError, match="执行器"):
        validate_task_contract(replace(plan, preferred_executor="cloud_root"), REGISTRY)
    with pytest.raises(PlanValidationError, match="扩大工作区"):
        validate_task_contract(replace(plan, expected_changes=("../outside.txt",)), REGISTRY)
