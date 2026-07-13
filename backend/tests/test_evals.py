import asyncio
import json
from pathlib import Path

import pytest

from app.evals.cli import main
from app.evals.comparison import compare_reports, evaluate_gate, load_policy, load_report, write_comparison
from app.evals.evidence import changed_paths, evaluate_rules, snapshot_workspace
from app.evals.loader import EvalContractError, default_tasks_path, load_tasks
from app.evals.models import EvalAction, EvalRule, EvalTaskSpec, GatePolicy
from app.evals.runner import run_evaluation


@pytest.fixture(scope="module")
def scripted_eval(tmp_path_factory):
    output = tmp_path_factory.mktemp("agent-eval")
    return asyncio.run(run_evaluation(label="pytest-core", output_root=output)), output


def test_fixed_task_contract_covers_all_roadmap_categories() -> None:
    tasks = load_tasks()
    assert len(tasks) == 18
    assert len({task.id for task in tasks}) == 18
    for task in tasks:
        assert task.task_type
        assert task.difficulty in {"easy", "medium", "hard"}
        assert task.max_execution_seconds > 0
        assert task.max_tokens > 0
        assert task.rules
        assert isinstance(task.expected_files, list)
        assert isinstance(task.validation_commands, list)
    selected = load_tasks(task_ids=["project-structure", "duplicate-tool-guard"])
    assert [task.id for task in selected] == ["project-structure", "duplicate-tool-guard"]
    with pytest.raises(EvalContractError, match="未知评测任务"):
        load_tasks(task_ids=["missing-task"])


def test_invalid_task_contract_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "bad.json"
    source.write_text('[{"id":"same"},{"id":"same"}]', encoding="utf-8")
    with pytest.raises(EvalContractError, match="合同无效"):
        load_tasks(source)
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(EvalContractError, match="JSON 数组"):
        load_tasks(source)


def test_workspace_evidence_uses_real_file_hashes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("before", encoding="utf-8")
    before = snapshot_workspace(tmp_path)
    (tmp_path / "a.txt").write_text("after", encoding="utf-8")
    (tmp_path / "b.txt").write_text("new", encoding="utf-8")
    after = snapshot_workspace(tmp_path)
    assert changed_paths(before, after) == ["a.txt", "b.txt"]

    spec = EvalTaskSpec(
        id="evidence-task",
        title="evidence",
        description="evidence",
        task_type="test",
        difficulty="easy",
        prompt="test",
        allow_write=True,
        expected_files=["a.txt", "b.txt"],
        actions=[EvalAction(kind="final", content="done")],
        rules=[EvalRule(kind="file_not_contains", path="a.txt", value="before"), EvalRule(kind="no_unrelated_changes")],
    )
    evidence = evaluate_rules(spec, tmp_path, {"tool_runs": [], "changed_files": ["a.txt", "b.txt"], "unrelated_files": []})
    assert all(item.passed for item in evidence)


def test_full_scripted_eval_generates_trace_reports_and_honest_baseline(scripted_eval) -> None:
    report, output = scripted_eval
    assert report.status == "completed"
    assert report.metrics["task_count"] == 18
    assert report.metrics["task_success_count"] == 17
    assert report.metrics["false_success_count"] == 0
    assert report.metrics["unrelated_file_modification_count"] == 0
    assert report.metrics["test_pass_rate"] == 1.0
    assert report.metrics["permission_violation_count"] == 0
    assert report.metrics["sandbox_violation_count"] == 0
    by_id = {item.task_id: item for item in report.task_results}
    assert by_id["interrupted-recovery"].expectation_met is False
    assert by_id["honest-block"].expectation_met is True
    assert by_id["honest-block"].false_success is False
    assert by_id["sidecar-interruption"].expectation_met is True
    assert by_id["timeout-and-cancel"].expectation_met is True
    assert by_id["fix-clear-bug"].trace["tool_runs"]
    assert by_id["fix-clear-bug"].trace["plans"]
    assert by_id["fix-clear-bug"].trace["verification_attempts"]

    json_path = Path(report.report_paths["json"])
    markdown_path = Path(report.report_paths["markdown"])
    assert json_path.is_file()
    assert markdown_path.is_file()
    assert (output / "history.jsonl").is_file()
    reloaded = load_report(json_path)
    assert reloaded.run_id == report.run_id
    assert "interrupted-recovery" in markdown_path.read_text(encoding="utf-8")

    no_regression = compare_reports(report, reloaded)
    assert no_regression["has_regressions"] is False
    paths = write_comparison(no_regression, output / "comparison")
    assert Path(paths["json"]).is_file()
    assert Path(paths["markdown"]).is_file()

    default_gate = evaluate_gate(report, load_policy())
    assert default_gate["passed"] is True
    relaxed_gate = evaluate_gate(report, GatePolicy(max_false_success_rate=0.1))
    assert relaxed_gate["passed"] is True


def test_comparison_detects_metric_and_task_regressions(scripted_eval) -> None:
    report, _ = scripted_eval
    candidate = report.model_copy(deep=True)
    candidate.run_id = "candidate"
    candidate.label = "candidate"
    candidate.metrics["task_success_rate"] = 0.1
    candidate.metrics["false_success_rate"] = 0.5
    first_passed = next(item for item in candidate.task_results if item.expectation_met)
    first_passed.expectation_met = False
    first_passed.status = "failed"
    comparison = compare_reports(report, candidate)
    assert comparison["has_regressions"] is True
    assert any(item.get("task_id") == first_passed.task_id for item in comparison["regressions"])
    assert evaluate_gate(candidate, GatePolicy(), comparison)["passed"] is False


def test_live_eval_without_key_is_explicitly_blocked(tmp_path: Path) -> None:
    report = asyncio.run(run_evaluation(label="no-key", mode="live_model", output_root=tmp_path, api_key=None))
    assert report.status == "blocked"
    assert all(item.status == "blocked" for item in report.task_results)
    assert report.metrics["task_success_count"] == 0


def test_cli_validates_contract(capsys) -> None:
    assert main(["validate", "--tasks", str(default_tasks_path())]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["task_count"] == 18
