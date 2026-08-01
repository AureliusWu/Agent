from dataclasses import dataclass

import pytest

from app.cognition.task_intelligence import (
    DependencyNode,
    allocate_task_budget,
    classify_complexity,
    classify_task_type,
    extract_task_requirements,
    validate_dependency_graph,
)


def test_classifies_professional_task_types() -> None:
    assert classify_task_type("修复 app.py 并运行测试") == "coding"
    assert classify_task_type("分析 CSV 指标和缺失值") == "data"
    assert classify_task_type("整理目录并移动文件") == "file_operation"


def test_extracts_functional_and_hard_constraint_requirements() -> None:
    requirements = extract_task_requirements("实现任务图。\n不得使用付费模型。")
    assert [item.requirement_type for item in requirements] == ["functional", "constraint"]
    assert all(item.required for item in requirements)


def test_rejects_cycles_unresolved_dependencies_and_parallel_write_conflicts() -> None:
    with pytest.raises(ValueError, match="cycle"):
        validate_dependency_graph((DependencyNode("a", ("b",), (), 1, 1), DependencyNode("b", ("a",), (), 1, 1)))
    with pytest.raises(ValueError, match="unresolved"):
        validate_dependency_graph((DependencyNode("a", ("missing",), (), 1, 1),))
    with pytest.raises(ValueError, match="write conflict"):
        validate_dependency_graph((DependencyNode("a", (), ("same.txt",), 1, 1), DependencyNode("b", (), ("same.txt",), 1, 1)))


def test_budget_is_bounded_and_allocated_per_step() -> None:
    budget = allocate_task_budget("simple", 99_999, ("a", "b"))
    assert budget.total_tokens == 8_000
    assert budget.allocation == {"a": 4_000, "b": 4_000}
    assert classify_complexity("完整架构升级并发布回归", 7) == "complex"
