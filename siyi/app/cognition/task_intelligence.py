from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from app.database import connect, now_iso, rows


TaskType = Literal["coding", "data", "document", "file_operation", "research", "general"]
Complexity = Literal["simple", "moderate", "complex"]


@dataclass(frozen=True)
class TaskRequirement:
    id: str
    description: str
    requirement_type: str
    source: str = "user"
    required: bool = True


@dataclass(frozen=True)
class AcceptanceCondition:
    id: str
    description: str
    verifier: str
    evidence_required: bool = True


@dataclass(frozen=True)
class DependencyNode:
    id: str
    depends_on: tuple[str, ...]
    write_scope: tuple[str, ...]
    estimated_tokens: int
    estimated_seconds: int


@dataclass(frozen=True)
class TaskBudget:
    total_tokens: int
    total_seconds: int
    model_calls: int
    tool_calls: int
    allocation: dict[str, int]


@dataclass(frozen=True)
class TaskIntelligence:
    task_type: TaskType
    complexity: Complexity
    requirements: tuple[TaskRequirement, ...]
    acceptance_conditions: tuple[AcceptanceCondition, ...]
    dependencies: tuple[DependencyNode, ...]
    budget: TaskBudget


_TYPE_SIGNALS: tuple[tuple[TaskType, tuple[str, ...]], ...] = (
    ("coding", ("代码", "修复", "实现", "测试", "build", "bug", ".py", ".ts", ".tsx")),
    ("data", ("数据", "分析", "csv", "xlsx", "统计", "指标", "sql")),
    ("document", ("文档", "报告", "方案", "markdown", "docx", "pdf", "总结")),
    ("file_operation", ("文件", "目录", "移动", "重命名", "整理", "复制", "删除")),
    ("research", ("调研", "搜索", "比较", "资料", "研究")),
)


def classify_task_type(prompt: str) -> TaskType:
    normalized = prompt.casefold()
    scores = {task_type: sum(signal in normalized for signal in signals) for task_type, signals in _TYPE_SIGNALS}
    task_type, score = max(scores.items(), key=lambda item: item[1])
    return task_type if score else "general"


def classify_complexity(prompt: str, step_count: int = 1) -> Complexity:
    normalized = prompt.casefold()
    complex_signals = ("全部", "完整", "迁移", "架构", "发布", "回归", "性能", "安全", "多智能体")
    if step_count >= 6 or len(prompt) >= 500 or sum(item in normalized for item in complex_signals) >= 3:
        return "complex"
    if step_count >= 3 or len(prompt) >= 120 or any(item in normalized for item in complex_signals):
        return "moderate"
    return "simple"


def extract_task_requirements(prompt: str, constraints: Iterable[str] = ()) -> tuple[TaskRequirement, ...]:
    fragments = [part.strip(" -\t") for part in re.split(r"[\r\n]+|(?<=[。；;])", prompt) if part.strip(" -\t")]
    result: list[TaskRequirement] = []
    for index, fragment in enumerate(fragments[:24], start=1):
        kind = "constraint" if any(token in fragment for token in ("不得", "不要", "仅", "必须", "限制")) else "functional"
        result.append(TaskRequirement(f"req-{index}", fragment, kind))
    for index, constraint in enumerate(constraints, start=len(result) + 1):
        value = str(constraint).strip()
        if value and all(item.description != value for item in result):
            result.append(TaskRequirement(f"req-{index}", value, "constraint", source="planner"))
    return tuple(result or (TaskRequirement("req-1", prompt.strip(), "functional"),))


def extract_acceptance_conditions(criteria: Iterable[Any], requirements: Iterable[TaskRequirement]) -> tuple[AcceptanceCondition, ...]:
    conditions: list[AcceptanceCondition] = []
    for index, criterion in enumerate(criteria, start=1):
        description = str(getattr(criterion, "description", criterion)).strip()
        if description:
            verifier = str(getattr(criterion, "kind", "evidence"))
            conditions.append(AcceptanceCondition(f"accept-{index}", description, verifier))
    if not conditions:
        for index, requirement in enumerate(requirements, start=1):
            conditions.append(AcceptanceCondition(f"accept-{index}", f"已满足：{requirement.description}", "evidence"))
    return tuple(conditions)


def build_dependency_graph(steps: Iterable[Any], total_tokens: int) -> tuple[DependencyNode, ...]:
    materialized = tuple(steps)
    per_step = max(1, total_tokens // max(1, len(materialized)))
    nodes = tuple(
        DependencyNode(
            id=str(getattr(step, "id")),
            depends_on=tuple(getattr(step, "depends_on", ()) or ()),
            write_scope=tuple(getattr(step, "expected_paths", ()) or ()),
            estimated_tokens=per_step,
            estimated_seconds=max(5, per_step // 40),
        )
        for step in materialized
    )
    validate_dependency_graph(nodes)
    return nodes


def validate_dependency_graph(nodes: Iterable[DependencyNode]) -> None:
    materialized = tuple(nodes)
    graph = {node.id: node for node in materialized}
    if len(graph) != len(materialized):
        raise ValueError("dependency node id is duplicated")
    for node in graph.values():
        missing = set(node.depends_on) - set(graph)
        if missing:
            raise ValueError(f"unresolved dependencies: {sorted(missing)}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("dependency graph contains a cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in graph[node_id].depends_on:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in graph:
        visit(node_id)
    parallel = [node for node in graph.values() if not node.depends_on]
    owners: dict[str, str] = {}
    for node in parallel:
        for path in node.write_scope:
            if path in owners:
                raise ValueError(f"parallel write conflict: {path}")
            owners[path] = node.id


def allocate_task_budget(complexity: Complexity, requested_tokens: int, step_ids: Iterable[str]) -> TaskBudget:
    caps = {"simple": (8_000, 300, 8, 24), "moderate": (32_000, 1_200, 24, 120), "complex": (120_000, 7_200, 80, 500)}
    token_cap, seconds, model_calls, tool_calls = caps[complexity]
    total_tokens = max(1, min(requested_tokens or token_cap, token_cap))
    ids = tuple(step_ids)
    per_step = max(1, total_tokens // max(1, len(ids)))
    return TaskBudget(total_tokens, seconds, model_calls, tool_calls, {step_id: per_step for step_id in ids})


def analyze_task_plan(plan: Any) -> TaskIntelligence:
    complexity = classify_complexity(plan.goal, len(plan.steps))
    requirements = extract_task_requirements(plan.goal, plan.constraints)
    acceptance = extract_acceptance_conditions(plan.acceptance_criteria, requirements)
    budget = allocate_task_budget(complexity, int(plan.budget_limit or 0), (step.id for step in plan.steps))
    dependencies = build_dependency_graph(plan.steps, budget.total_tokens)
    return TaskIntelligence(classify_task_type(plan.goal), complexity, requirements, acceptance, dependencies, budget)


def persist_task_intelligence(task_id: str, intelligence: TaskIntelligence) -> None:
    stamp = now_iso()
    with connect() as db:
        db.execute("DELETE FROM task_requirements WHERE task_id=?", (task_id,))
        db.execute("DELETE FROM task_acceptance_conditions WHERE task_id=?", (task_id,))
        db.execute("DELETE FROM task_dependencies WHERE task_id=?", (task_id,))
        db.executemany(
            "INSERT INTO task_requirements(id,task_id,description,requirement_type,source,required,created_at) VALUES(?,?,?,?,?,?,?)",
            ((f"{task_id}:{item.id}", task_id, item.description, item.requirement_type, item.source, int(item.required), stamp) for item in intelligence.requirements),
        )
        db.executemany(
            "INSERT INTO task_acceptance_conditions(id,task_id,description,verifier,evidence_required,created_at) VALUES(?,?,?,?,?,?)",
            ((f"{task_id}:{item.id}", task_id, item.description, item.verifier, int(item.evidence_required), stamp) for item in intelligence.acceptance_conditions),
        )
        db.executemany(
            "INSERT INTO task_dependencies(task_id,node_id,depends_on,write_scope,estimated_tokens,estimated_seconds,created_at) VALUES(?,?,?,?,?,?,?)",
            ((task_id, item.id, json.dumps(item.depends_on), json.dumps(item.write_scope), item.estimated_tokens, item.estimated_seconds, stamp) for item in intelligence.dependencies),
        )
        db.execute(
            "INSERT INTO task_budgets(task_id,total_tokens,total_seconds,model_calls,tool_calls,allocation,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET total_tokens=excluded.total_tokens,total_seconds=excluded.total_seconds,model_calls=excluded.model_calls,tool_calls=excluded.tool_calls,allocation=excluded.allocation,updated_at=excluded.updated_at",
            (task_id, intelligence.budget.total_tokens, intelligence.budget.total_seconds, intelligence.budget.model_calls, intelligence.budget.tool_calls, json.dumps(intelligence.budget.allocation), stamp),
        )


def task_intelligence_snapshot(task_id: str) -> dict[str, Any]:
    return {
        "requirements": rows("SELECT description,requirement_type,source,required FROM task_requirements WHERE task_id=? ORDER BY id", (task_id,)),
        "acceptance_conditions": rows("SELECT description,verifier,evidence_required FROM task_acceptance_conditions WHERE task_id=? ORDER BY id", (task_id,)),
        "dependencies": rows("SELECT node_id,depends_on,write_scope,estimated_tokens,estimated_seconds FROM task_dependencies WHERE task_id=? ORDER BY node_id", (task_id,)),
        "budget": next(iter(rows("SELECT total_tokens,total_seconds,model_calls,tool_calls,allocation FROM task_budgets WHERE task_id=?", (task_id,))), None),
    }
