import asyncio
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from app.database import connect, now_iso
from app.cognition.planning import PlanValidationError, build_task_plan, guard_task_contract, load_task_plan, save_task_plan, validate_task_contract
from app.cognition.semantic_planner import PlannerContext, build_semantic_task_plan
from app.context.budget import ModelContextProfile, RequestBudget, request_budget
from app.efficiency import TokenBudget
from app.providers.configuration import ProviderConfiguration
from app.providers.provider import ProviderError
from app.runtime.cost_budget import COST_ERRORS
from app.tools.registry import REGISTRY, select_model_tools


@pytest.fixture(autouse=True)
def isolated_planner_model_budget(monkeypatch):
    """Planner unit tests never construct a provider or read its user store."""
    configuration = ProviderConfiguration(provider_id="mock")
    monkeypatch.setattr("app.cognition.semantic_planner.load_provider_configuration", lambda: configuration)

    def profile(*, base_url=None, model=None, configuration=None):
        assert configuration is not None
        return ModelContextProfile(
            provider_id=configuration.provider_id,
            model_id=model or configuration.model or "siyi-mock-v1",
            context_window_tokens=65_536,
            max_output_tokens=configuration.max_tokens,
            supports_tools=True,
            supports_parallel_tools=False,
            supports_json_schema=False,
            supports_streaming=False,
            supports_usage_reporting=True,
            capability_source="deterministic_contract",
            window_status="declared",
            local=configuration.provider_id in {"mock", "ollama", "openai_compatible"},
        )

    monkeypatch.setattr("app.context.budget.model_context_profile", profile)
    return profile


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


@pytest.mark.parametrize(
    "prompt,path",
    [
        ("在 math_utils.py 中添加 multiply(a, b) 函数并验证。", "math_utils.py"),
        ("在 `math_utils.py` 中添加 multiply(a, b) 函数并验证。", "math_utils.py"),
        ("在`math_utils.py`中修改 multiply 函数并验证。", "math_utils.py"),
        ("在 src/math_utils.py 内修改 multiply 函数并验证。", "src/math_utils.py"),
    ],
)
def test_file_subject_code_write_requires_mandatory_command_verification(prompt: str, path: str) -> None:
    plan = build_task_plan("file-subject-code-write", prompt, REGISTRY)

    assert plan.expected_changes == (path,)
    assert "command.execute" in plan.required_capabilities
    criteria = {criterion.id: criterion for criterion in plan.acceptance_criteria}
    assert criteria["verification_command"].required is True
    assert criteria["verify_code"].required is True
    verify = next(step for step in plan.steps if step.id == "verify")
    assert verify.tools == ("run_command",)
    assert verify.depends_on == ("execute",)
    assert verify.risk == "critical"
    planned_tools = tuple(tool for step in plan.steps for tool in step.tools)
    assert "run_command" in {tool["function"]["name"] for tool in select_model_tools(prompt, planned_tools, [])}


@pytest.mark.parametrize(
    "prompt",
    [
        "读取 old.py；修改 new.py。",
        "读取 old.py 再修改 new.py。",
        "读取 old.py，再在 `new.py` 中添加 multiply 函数并验证。",
    ],
)
def test_file_subject_write_keeps_prior_read_only_code_out_of_write_targets(prompt: str) -> None:
    plan = build_task_plan("read-old-write-new", prompt, REGISTRY)

    assert plan.expected_paths == ("old.py", "new.py")
    assert plan.expected_changes == ("new.py",)
    assert [criterion.parameters["path"] for criterion in plan.acceptance_criteria if criterion.kind == "path_state"] == ["new.py"]
    assert "command.execute" in plan.required_capabilities


@pytest.mark.parametrize(
    "prompt",
    [
        "读取 old.py；创建 report.txt 并写入摘要。不要修改其他文件。",
        "读取 old.py；在 report.txt 中添加摘要。不要修改其他文件。",
    ],
)
def test_file_subject_text_report_does_not_inherit_command_capability_from_read_code(prompt: str) -> None:
    plan = build_task_plan("read-code-write-text-report", prompt, REGISTRY)

    assert plan.expected_changes == ("report.txt",)
    assert "command.execute" not in plan.required_capabilities
    assert "verify_code" not in {criterion.id for criterion in plan.acceptance_criteria}
    assert "run_command" not in {tool for step in plan.steps for tool in step.tools}


@pytest.mark.parametrize(
    "prompt",
    [
        "不要修改 math_utils.py。",
        "不要在 `math_utils.py` 中添加函数。",
        "在 `math_utils.py` 中不要添加函数。",
        "给出 math_utils.py 的修改计划，不要修改文件。",
        "验证 notes.txt 是否存在，不要修改文件。",
        "验证 math_utils.py 是否存在，不要修改文件。",
    ],
)
def test_file_subject_negation_plan_and_file_presence_checks_do_not_authorize_writes_or_commands(prompt: str) -> None:
    plan = build_task_plan("file-subject-read-only", prompt, REGISTRY)

    assert "workspace.write" not in plan.required_capabilities
    assert "command.execute" not in plan.required_capabilities
    assert plan.expected_changes == ()
    assert "verification_command" not in {criterion.id for criterion in plan.acceptance_criteria}
    assert "run_command" not in {tool for step in plan.steps for tool in step.tools}


@pytest.mark.parametrize(
    "prompt",
    [
        "给出在 `math_utils.py` 中添加函数的计划，不要修改文件。",
        "仅给出在 `math_utils.py` 中添加函数并运行测试的计划，不要修改文件。",
    ],
)
def test_file_subject_write_proposal_remains_plan_only_without_write_or_command_capabilities(prompt: str) -> None:
    plan = build_task_plan("file-subject-proposal-only", prompt, REGISTRY)

    assert plan.expected_changes == ()
    assert plan.required_capabilities == ("workspace.read",)
    assert "verification_command" not in {criterion.id for criterion in plan.acceptance_criteria}
    assert "run_command" not in {tool for step in plan.steps for tool in step.tools}


@pytest.mark.parametrize(
    "prompt,path,needs_command",
    [
        ("把 config.json 的 debug 改为 false，保留其他配置。", "config.json", False),
        ("把 settings.py 的 TIMEOUT 改为 20；工具失败时先检查原因再恢复，并运行验证。", "settings.py", True),
        ("把 preferences.json 的 compact 改为 true。", "preferences.json", False),
        ("把 greeting.py 的问候语优化得更清楚，控制修改范围。", "greeting.py", True),
        ("为 app.js 增加可导出的 toggleTheme 函数，不改动其他文件，并运行检查。", "app.js", True),
    ],
)
def test_core_object_first_write_prompts_bind_only_the_explicit_target(prompt: str, path: str, needs_command: bool) -> None:
    plan = build_task_plan("core-object-first-write", prompt, REGISTRY)

    assert plan.expected_changes == (path,)
    assert "workspace.write" in plan.required_capabilities
    assert ("command.execute" in plan.required_capabilities) is needs_command
    criteria = {criterion.id: criterion for criterion in plan.acceptance_criteria}
    assert ("verification_command" in criteria) is needs_command
    assert ("verify_code" in criteria) is needs_command
    if needs_command:
        assert criteria["verification_command"].required is True and criteria["verify_code"].required is True
    assert any("run_command" in step.tools for step in plan.steps) is needs_command


@pytest.mark.parametrize(
    "prompt,path,needs_command",
    [
        ("读取 old.py；把 new.py 的 TIMEOUT 改为 20。", "new.py", True),
        ("读取 old.py，再把 `new.py` 的问候语优化得更清楚。", "new.py", True),
        ("读取 old.py；为 `new.js` 增加可导出的 toggleTheme 函数。", "new.js", True),
        ("读取 old.py；把 config.json 的 debug 改为 false。", "config.json", False),
        ("把 notes.txt 的标题改为 新标题。", "notes.txt", False),
    ],
)
def test_object_first_write_does_not_promote_read_context_or_text_to_code_commands(prompt: str, path: str, needs_command: bool) -> None:
    plan = build_task_plan("object-first-scope", prompt, REGISTRY)

    assert plan.expected_changes == (path,)
    assert [criterion.parameters["path"] for criterion in plan.acceptance_criteria if criterion.kind == "path_state"] == [path]
    assert "workspace.write" in plan.required_capabilities
    assert ("command.execute" in plan.required_capabilities) is needs_command
    assert ("verify_code" in {criterion.id for criterion in plan.acceptance_criteria}) is needs_command


@pytest.mark.parametrize(
    "prompt",
    [
        "不要把 `greeting.py` 的问候语优化得更清楚。",
        "不要把 `settings.py` 的 TIMEOUT 改为 20。",
        "不要为 `app.js` 增加可导出的 toggleTheme 函数。",
        "把 `greeting.py` 的问候语不要优化。",
        "把 `settings.py` 的 TIMEOUT 不要改为 20。",
        "仅给出把 `greeting.py` 的问候语优化得更清楚的计划，不要修改文件。",
        "仅给出把 `settings.py` 的 TIMEOUT 改为 20 并运行测试的计划，不要修改文件。",
        "仅给出为 `app.js` 增加可导出的 toggleTheme 函数并运行检查的计划，不要修改文件。",
    ],
)
def test_object_first_write_negation_and_plan_only_never_authorize_write_or_commands(prompt: str) -> None:
    plan = build_task_plan("object-first-deny-and-plan", prompt, REGISTRY)

    assert plan.expected_changes == ()
    assert plan.required_capabilities == ("workspace.read",)
    assert "verification_command" not in {criterion.id for criterion in plan.acceptance_criteria}
    assert "run_command" not in {tool for step in plan.steps for tool in step.tools}


def test_adding_pytest_for_a_source_file_does_not_reclassify_the_source_as_a_write_target() -> None:
    plan = build_task_plan("pytest-context-not-source-write", "为 slugify.py 添加 pytest 测试并运行测试。", REGISTRY)

    assert plan.expected_paths == ("slugify.py",)
    assert plan.expected_changes == ()
    assert plan.required_capabilities == ("workspace.read", "workspace.write", "command.execute")
    assert {criterion.id for criterion in plan.acceptance_criteria if criterion.required} >= {"verification_command", "verify_code"}


def test_semantic_same_name_verify_step_cannot_remove_trusted_command_or_weaken_risk_and_dependencies() -> None:
    baseline = build_task_plan("semantic-verify-floor", "在 math_utils.py 中添加 multiply(a, b) 函数并验证。", REGISTRY)
    plan = guard_task_contract(
        baseline,
        {"steps": [{"id": "verify", "description": "仅宣称检查完成", "tools": [], "risk": "low"}], "acceptance_criteria": []},
        REGISTRY,
        interaction_mode="agent", data_location="local_workspace", privacy_scope="workspace",
        budget_limit=30000, preferred_model="deepseek-flash", memory_write_policy="explicit",
    )

    assert "command.execute" in plan.required_capabilities
    verify = next(step for step in plan.steps if step.id == "verify")
    assert verify.tools == ("run_command",)
    assert verify.depends_on == ("execute",)
    assert verify.risk == "critical"
    assert {criterion.id for criterion in plan.acceptance_criteria if criterion.required} >= {"verification_command", "verify_code"}


def test_semantic_verify_floor_cycle_is_rejected_and_uses_existing_deterministic_fallback() -> None:
    proposal = {
        "steps": [
            {"id": "verify", "description": "先宣称已验证", "tools": [], "risk": "low"},
            {"id": "execute", "description": "等待验证后才写入", "depends_on": ["verify"], "tools": ["apply_patch"], "risk": "medium"},
        ],
    }
    prompt = "在 math_utils.py 中添加 multiply(a, b) 函数并验证。"
    baseline = build_task_plan("semantic-verify-cycle", prompt, REGISTRY)
    with pytest.raises(PlanValidationError, match="循环"):
        guard_task_contract(
            baseline, proposal, REGISTRY,
            interaction_mode="agent", data_location="local_workspace", privacy_scope="workspace",
            budget_limit=30000, preferred_model="deepseek-flash", memory_write_policy="explicit",
        )

    async def cyclic_planner(messages, api_key=None, **kwargs):
        import json

        return {"role": "assistant", "content": json.dumps(proposal)}

    result = asyncio.run(build_semantic_task_plan(
        "semantic-verify-cycle-fallback", prompt, REGISTRY, complete=cyclic_planner, api_key=None,
        context=PlannerContext(budget_limit=30000, preferred_model="deepseek-flash"),
    ))
    assert result.plan.planner_source == "deterministic_fallback"
    assert "PlanValidationError" in (result.fallback_reason or "")
    verify = next(step for step in result.plan.steps if step.id == "verify")
    assert verify.tools == ("run_command",) and verify.depends_on == ("execute",)
    assert "command.execute" in result.plan.required_capabilities


def test_semantic_text_write_cannot_escalate_into_command_execution() -> None:
    baseline = build_task_plan("semantic-no-command-escalation", "读取 old.py；创建 report.txt 并写入摘要。", REGISTRY)
    plan = guard_task_contract(
        baseline,
        {"steps": [{"id": "extra", "description": "运行模型提出的命令", "tools": ["run_command"], "risk": "critical"}],
         "required_capabilities": ["workspace.write", "command.execute"], "verification_commands": ["python -m pytest"]},
        REGISTRY,
        interaction_mode="agent", data_location="local_workspace", privacy_scope="workspace",
        budget_limit=30000, preferred_model="deepseek-flash", memory_write_policy="explicit",
    )

    assert plan.required_capabilities == baseline.required_capabilities
    assert "command.execute" not in plan.required_capabilities
    assert "run_command" not in {tool for step in plan.steps for tool in step.tools}


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


@pytest.mark.parametrize("effort,expected", [("auto", None), ("low", "low"), ("medium", "medium"), ("high", "high")])
def test_semantic_planner_preserves_explicit_effort_and_model(effort: str, expected: str | None) -> None:
    calls: list[dict] = []

    async def complete(messages, api_key=None, **kwargs):
        calls.append(kwargs)
        return {"content": '{"steps":[],"acceptance_criteria":[]}'}

    result = asyncio.run(build_semantic_task_plan(
        "planner-effort", "分析项目结构，不要修改文件。", REGISTRY,
        complete=complete, api_key=None,
        context=PlannerContext(preferred_model="explicit-model", reasoning_effort=effort),
    ))

    assert result.plan.planner_source == "semantic_model"
    assert result.plan.preferred_model == "explicit-model"
    assert result.budget_reason is None
    assert len(calls) == 1
    assert calls[0]["model"] == "explicit-model"
    assert calls[0]["reasoning_effort"] == expected
    assert calls[0]["phase"] == "planning"
    assert calls[0]["tools"] is None


@pytest.mark.parametrize(
    "model_limit,call_limit,provider_limit,expected",
    [(12_000, 16_000, 20_000, 12_000), (15_000, 10_000, 20_000, 10_000), (15_000, 16_000, 6000, 6000)],
)
def test_semantic_planner_output_uses_configured_limits_not_fixed_task_cap(
    monkeypatch, model_limit: int, call_limit: int, provider_limit: int, expected: int,
) -> None:
    monkeypatch.setattr("app.cognition.semantic_planner.settings.model_max_tokens", model_limit)
    monkeypatch.setattr("app.cognition.semantic_planner.settings.max_model_call_tokens", call_limit)
    calls: list[dict] = []

    async def complete(messages, api_key=None, **kwargs):
        calls.append(kwargs)
        return {"content": "{}"}

    result = asyncio.run(build_semantic_task_plan(
        "planner-config-cap", "分析项目结构，不要修改文件。", REGISTRY,
        complete=complete, api_key=None, context=PlannerContext(budget_limit=500),
        provider_configuration=ProviderConfiguration(provider_id="mock", max_tokens=provider_limit),
    ))

    assert result.budget_reason is None
    assert calls[0]["max_tokens"] == expected > 4096
    assert calls[0]["reserved_output_tokens"] == expected
    assert calls[0]["estimated_input_tokens"] > 0
    assert result.plan.budget_limit == 500


def test_semantic_planner_honors_effective_model_window_cap(monkeypatch, isolated_planner_model_budget) -> None:
    def smaller_model_window(**kwargs):
        return replace(isolated_planner_model_budget(**kwargs), context_window_tokens=16_000, max_output_tokens=3000)

    monkeypatch.setattr("app.context.budget.model_context_profile", smaller_model_window)
    calls: list[dict] = []

    async def complete(messages, api_key=None, **kwargs):
        calls.append(kwargs)
        return {"content": "{}"}

    result = asyncio.run(build_semantic_task_plan(
        "planner-model-window", "分析项目结构，不要修改文件。", REGISTRY,
        complete=complete, api_key=None, context=PlannerContext(),
    ))

    assert result.budget_reason is None
    assert calls[0]["max_tokens"] == 3000
    assert calls[0]["context_window_tokens"] == 16_000


@pytest.mark.parametrize("hard_limit", [False, True])
def test_semantic_planner_preflights_the_shared_budget_without_resetting_usage(hard_limit: bool) -> None:
    calls: list[dict] = []
    preflights: list[tuple[str, int, int]] = []

    class SharedBudget(TokenBudget):
        def preflight(self, phase, estimated_input_tokens, route_limit):
            preflights.append((phase, estimated_input_tokens, route_limit))
            return super().preflight(phase, estimated_input_tokens, route_limit)

    budget = SharedBudget(
        total_limit=13_000, phase_limit=10_000, call_limit=7000,
        total_tokens=5000, phase_tokens={"planning": 4000}, hard_limit=hard_limit,
    )

    async def complete(messages, api_key=None, **kwargs):
        calls.append(kwargs)
        return {"content": "{}"}

    result = asyncio.run(build_semantic_task_plan(
        "planner-shared-budget", "分析项目结构，不要修改文件。", REGISTRY,
        complete=complete, api_key=None, context=PlannerContext(), token_budget=budget,
    ))

    assert result.budget_reason is None
    assert len(preflights) == len(calls) == 1
    phase, input_estimate, allowance = preflights[0]
    assert phase == "planning"
    assert allowance == 7000
    expected = min(7000, 13_000 - 5000 - input_estimate, 10_000 - 4000 - input_estimate) if hard_limit else 7000
    assert calls[0]["max_tokens"] == expected
    assert budget.total_tokens == 5000
    assert budget.phase_tokens == {"planning": 4000}


def test_semantic_planner_soft_task_pressure_does_not_remove_runtime_call_ceiling() -> None:
    calls: list[dict] = []
    budget = TokenBudget(
        total_limit=10, phase_limit=10, call_limit=1500,
        total_tokens=100, phase_tokens={"planning": 100}, hard_limit=False,
    )

    async def complete(messages, api_key=None, **kwargs):
        calls.append(kwargs)
        return {"content": "{}"}

    result = asyncio.run(build_semantic_task_plan(
        "planner-soft-pressure", "分析项目结构，不要修改文件。", REGISTRY,
        complete=complete, api_key=None, context=PlannerContext(budget_limit=10), token_budget=budget,
    ))

    assert result.budget_reason is None
    assert calls[0]["max_tokens"] == 1500


@pytest.mark.parametrize(
    "configuration,preferred_model,expected_budget_model",
    [
        (ProviderConfiguration(provider_id="deepseek", model="configured-model"), "explicit-model", "explicit-model"),
        (ProviderConfiguration(provider_id="ollama", model="qwen3:4b"), "remote-choice", "qwen3:4b"),
        (ProviderConfiguration(provider_id="openai_compatible", base_url="https://provider.example/v1", model="locked-model"), "remote-choice", "locked-model"),
        (ProviderConfiguration(provider_id="mock"), "remote-choice", ""),
    ],
)
def test_semantic_planner_budget_binding_matches_the_actual_provider_model(
    monkeypatch, configuration: ProviderConfiguration, preferred_model: str, expected_budget_model: str,
) -> None:
    admissions: list[dict] = []
    completions: list[dict] = []

    def admit(messages, tools, **kwargs):
        admissions.append(kwargs)
        return request_budget(messages, tools, **kwargs)

    async def complete(messages, api_key=None, **kwargs):
        completions.append(kwargs)
        return {"content": "{}"}

    monkeypatch.setattr("app.cognition.semantic_planner.request_budget", admit)
    result = asyncio.run(build_semantic_task_plan(
        "planner-model-binding", "分析项目结构，不要修改文件。", REGISTRY,
        complete=complete, api_key=None, context=PlannerContext(preferred_model=preferred_model),
        provider_configuration=configuration,
    ))

    assert result.budget_reason is None
    assert len(admissions) == len(completions) == 1
    assert admissions[0]["configuration"] is configuration
    assert admissions[0]["model"] == expected_budget_model
    assert "base_url" not in admissions[0]
    assert completions[0]["model"] == preferred_model


@pytest.mark.parametrize("failure", ["unknown_window", "overflow", "no_output", "hard_budget", "configuration", "admission"])
def test_semantic_planner_failed_admission_never_calls_a_model(monkeypatch, failure: str) -> None:
    calls: list[dict] = []
    budget = RequestBudget(
        estimated_input_tokens=100, context_window_tokens=10_000, reserved_output_tokens=1000,
        provider_overhead_tokens=100, safety_margin_tokens=100, effective_input_budget=8800,
        compaction_threshold_tokens=7000, should_compact=False, exceeds_context_window=False,
    )
    if failure == "unknown_window":
        budget = replace(budget, blocked_reason="context_window_unknown")
    elif failure == "overflow":
        budget = replace(budget, exceeds_context_window=True)
    elif failure == "no_output":
        budget = replace(budget, reserved_output_tokens=0)

    def admit(*args, **kwargs):
        if failure == "admission":
            raise ValueError("private-admission-detail")
        return budget

    def configuration():
        raise ValueError("private-configuration-detail")

    async def complete(messages, api_key=None, **kwargs):
        calls.append(kwargs)
        raise AssertionError("A rejected planner must not call complete")

    monkeypatch.setattr("app.cognition.semantic_planner.request_budget", admit)
    if failure == "configuration":
        monkeypatch.setattr("app.cognition.semantic_planner.load_provider_configuration", configuration)
    shared = TokenBudget(total_limit=50, phase_limit=50, call_limit=1000) if failure == "hard_budget" else None
    result = asyncio.run(build_semantic_task_plan(
        "planner-admission-denied", "分析项目结构，不要修改文件。", REGISTRY,
        complete=complete, api_key=None, context=PlannerContext(), token_budget=shared,
    ))

    assert calls == []
    assert result.budget_reason
    assert "private-" not in result.budget_reason
    assert result.fallback_reason is None
    assert result.metrics == {}
    assert result.plan.planner_source == "deterministic_fallback"
    assert result.plan.requires_user_input is True
    assert result.plan.required_capabilities == ("workspace.read",)


@pytest.mark.parametrize("invalid_proposal", ["json", "guard"])
def test_semantic_planner_keeps_billed_metrics_when_the_proposal_is_unusable(invalid_proposal: str) -> None:
    metrics = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, "estimated_cost_usd": 0.002}
    proposal = {
        "steps": [
            {"id": "verify", "description": "先验证", "tools": [], "risk": "low"},
            {"id": "execute", "description": "等待验证再执行", "depends_on": ["verify"], "tools": ["apply_patch"], "risk": "medium"},
        ],
    }
    calls: list[dict] = []

    async def complete(messages, api_key=None, **kwargs):
        calls.append(kwargs)
        return {"content": "not-json" if invalid_proposal == "json" else json.dumps(proposal), "_metrics": metrics}

    result = asyncio.run(build_semantic_task_plan(
        "planner-invalid-metrics", "在 math_utils.py 中添加 multiply(a, b) 函数并验证。", REGISTRY,
        complete=complete, api_key=None, context=PlannerContext(),
    ))

    assert len(calls) == 1
    assert result.budget_reason is None
    assert result.fallback_reason
    assert result.plan.planner_source == "deterministic_fallback"
    assert result.metrics == metrics
    assert "command.execute" in result.plan.required_capabilities
    assert result.plan.expected_changes == ("math_utils.py",)


def test_semantic_planner_keeps_only_the_provider_errors_internal_accounting_metrics() -> None:
    error = ProviderError("Planner 输出耗尽", "empty_after_reasoning", details={"usage": {"total_tokens": 25}})
    error._metrics = {
        "usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25},
        "estimated_cost_usd": None,
        "cost_status": "unknown",
    }

    async def complete(messages, api_key=None, **kwargs):
        raise error

    result = asyncio.run(build_semantic_task_plan(
        "planner-error-metrics", "分析项目结构，不要修改文件。", REGISTRY,
        complete=complete, api_key=None, context=PlannerContext(),
    ))

    assert result.plan.planner_source == "deterministic_fallback"
    assert result.metrics == error._metrics
    assert result.metrics["estimated_cost_usd"] is None
    assert "_metrics" not in error.as_dict()
    assert result.budget_reason is None


def test_semantic_planner_does_not_invent_metrics_from_raw_error_usage() -> None:
    async def complete(messages, api_key=None, **kwargs):
        raise ProviderError("输出不可用", "empty_response", details={"usage": {"total_tokens": 25}})

    result = asyncio.run(build_semantic_task_plan(
        "planner-no-fabricated-price", "分析项目结构，不要修改文件。", REGISTRY,
        complete=complete, api_key=None, context=PlannerContext(),
    ))

    assert result.plan.planner_source == "deterministic_fallback"
    assert result.metrics == {}
    assert result.budget_reason is None


@pytest.mark.parametrize("stage", ["admission", "completion"])
@pytest.mark.parametrize("error_type", sorted(COST_ERRORS))
def test_semantic_planner_never_swallows_cost_boundary_errors(monkeypatch, stage: str, error_type: str) -> None:
    error = ProviderError("成本边界不可用", error_type)
    error._metrics = {"usage": {"total_tokens": 10}, "estimated_cost_usd": None}
    calls: list[str] = []

    def admit(*args, **kwargs):
        raise error

    async def complete(messages, api_key=None, **kwargs):
        calls.append("completion")
        raise error

    if stage == "admission":
        monkeypatch.setattr("app.cognition.semantic_planner.request_budget", admit)
    with pytest.raises(ProviderError) as caught:
        asyncio.run(build_semantic_task_plan(
            "planner-cost-error", "分析项目结构，不要修改文件。", REGISTRY,
            complete=complete, api_key=None, context=PlannerContext(),
        ))

    assert caught.value is error
    assert calls == ([] if stage == "admission" else ["completion"])
