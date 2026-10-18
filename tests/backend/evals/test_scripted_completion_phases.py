import asyncio
import json
from pathlib import Path

import pytest

from app.config import settings
from app.evals.loader import load_tasks
from app.evals.models import EvalAction
from app.evals.runner import ScriptedCompletion, run_evaluation
from app.providers.configuration import ProviderConfiguration, save_provider_configuration
from app.providers.registry import provider_ready


PLANNING = {"phase": "planning", "task_type": "planning", "tools": None}
NEUTRAL_PLAN = {"steps": [], "expected_changes": [], "acceptance_criteria": []}
SYNTHETIC_USAGE = {"prompt_tokens": 32, "completion_tokens": 16, "total_tokens": 48}
TASK_IDS = ["project-structure", "permission-limited", "sandbox-escape", "honest-block", "duplicate-tool-guard"]


def test_semantic_planning_does_not_consume_the_first_execution_action() -> None:
    script = ScriptedCompletion([EvalAction(kind="tool", tool="read_file", arguments={"path": "README.md"})])
    planned = asyncio.run(script([], **PLANNING))

    assert script.position == 0
    assert script.call_count == 1
    assert json.loads(planned["content"]) == NEUTRAL_PLAN
    assert planned["_metrics"]["usage"] == SYNTHETIC_USAGE
    executed = asyncio.run(script([], phase="analysis"))
    assert executed["tool_calls"][0]["function"]["name"] == "read_file"
    assert script.position == 1
    assert script.call_count == 2


def test_repeated_and_post_exhaustion_planning_have_fresh_responses() -> None:
    script = ScriptedCompletion([EvalAction(kind="final", content="executed")])
    first = asyncio.run(script([], **PLANNING))
    first.pop("_metrics")["usage"]["total_tokens"] = -1
    second = asyncio.run(script([], **PLANNING))
    assert first is not second
    assert second["_metrics"]["usage"] == SYNTHETIC_USAGE
    assert script.position == 0
    assert script.call_count == 2
    assert asyncio.run(script([], phase="analysis"))["content"] == "executed"
    with pytest.raises(RuntimeError, match="评测脚本已耗尽"):
        asyncio.run(script([], phase="analysis"))
    final_plan = asyncio.run(script([], **PLANNING))
    assert json.loads(final_plan["content"]) == NEUTRAL_PLAN
    assert script.position == 1
    assert script.call_count == 4


@pytest.mark.parametrize(
    "model_kwargs",
    [
        {"phase": "planning", "task_type": "execution", "tools": []},
        {"phase": "planning", "task_type": "planning", "tools": []},
        {"phase": "planning", "tools": None},
        {"phase": "analysis", "task_type": "planning", "tools": None},
        {"phase": "multi_agent:planner", "task_type": "multi_agent", "tools": []},
        {"phase": "multi_agent:verifier", "tools": []},
        {"phase": "verification", "tools": []},
        {"phase": "repair", "tools": []},
        {"phase": "conversation", "tools": None},
        {"phase": "unknown-phase", "tools": []},
    ],
)
def test_non_semantic_requests_keep_the_execution_action_contract(model_kwargs: dict) -> None:
    script = ScriptedCompletion([EvalAction(kind="final", content="execution action")])
    assert asyncio.run(script([], **model_kwargs))["content"] == "execution action"
    assert script.position == 1
    assert script.call_count == 1


def test_retry_still_only_rewinds_the_execution_action_cursor() -> None:
    script = ScriptedCompletion([EvalAction(kind="final", content="first"), EvalAction(kind="final", content="second")])
    asyncio.run(script([], **PLANNING))
    script.retry_last_action()
    assert script.position == 0
    assert asyncio.run(script([], phase="analysis"))["content"] == "first"
    asyncio.run(script([], **PLANNING))
    assert script.position == 1
    script.retry_last_action()
    assert script.position == 0
    assert script.call_count == 3
    assert asyncio.run(script([], phase="repair"))["content"] == "first"
    assert script.call_count == 4


def test_mock_ready_scripted_eval_keeps_real_plans_tools_and_rejections(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))
    monkeypatch.setenv("SIYI_MOCK_SCENARIO", "normal")
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "false")
    monkeypatch.setattr(settings, "database_path", tmp_path / "unused.db")
    monkeypatch.setattr(settings, "log_path", tmp_path / "isolated.log")
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "tavily_api_key", "")
    monkeypatch.setattr(settings, "brave_api_key", "")
    save_provider_configuration(ProviderConfiguration(provider_id="mock", max_retries=0))
    assert provider_ready(None) is True

    async def reject_network(*_args, **_kwargs):
        raise AssertionError("scripted evaluation must not send HTTP requests")

    monkeypatch.setattr("httpx.AsyncClient.send", reject_network)
    monkeypatch.setattr("httpx.Client.send", lambda *_args, **_kwargs: pytest.fail("unexpected synchronous HTTP"))
    report = asyncio.run(run_evaluation(
        label="mock-ready-phase-regression", mode="scripted_runtime", suite="core",
        task_ids=TASK_IDS, output_root=tmp_path / "reports",
    ))

    assert report.status == "completed"
    assert report.metrics["task_count"] == report.metrics["task_success_count"] == 5
    assert report.metrics["false_success_count"] == 0
    assert report.metrics["permission_violation_count"] == 0
    assert report.metrics["sandbox_violation_count"] == 0
    assert report.metrics["unrelated_file_modification_count"] == 0
    by_id = {result.task_id: result for result in report.task_results}
    for result in report.task_results:
        assert result.expectation_met is True
        assert result.trace["plans"]
        assert all(plan["plan"]["planner_source"] == "semantic_model" for plan in result.trace["plans"])
    structure = by_id["project-structure"]
    assert [run["tool"] for run in structure.trace["tool_runs"]] == ["list_files", "read_file", "read_file"]
    structure_spec = next(spec for spec in load_tasks(task_ids=TASK_IDS) if spec.id == "project-structure")
    assert structure.trace["tasks"][0]["model_calls"] == len(structure_spec.actions) + 1
    assert structure.trace["tasks"][0]["total_tokens"] == (
        sum(action.input_tokens + action.output_tokens for action in structure_spec.actions) + 48
    )
    assert json.loads(structure.trace["tasks"][0]["phase_tokens"])["planning"] == 48
    assert by_id["permission-limited"].trace["tool_runs"][0]["tool"] == "read_file"
    assert any(item.rule == "sandbox_rejected" and item.passed for item in by_id["sandbox-escape"].evidence)
    assert by_id["honest-block"].runtime_status == "blocked"
    assert not list((tmp_path / "reports").glob(".run-*"))
