from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Iterable

from .models import CompanionEvalContract, EvalTaskSpec


class EvalContractError(ValueError):
    pass


def default_tasks_path() -> Path:
    return Path(__file__).resolve().parents[3] / "evals" / "tasks.json"


def default_companion_contracts_path() -> Path:
    return Path(__file__).resolve().parents[3] / "evals" / "companion_contracts.json"


def load_companion_contracts(path: str | Path | None = None) -> list[CompanionEvalContract]:
    source = Path(path) if path else default_companion_contracts_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalContractError(f"Unable to read companion eval contracts {source}: {exc}") from exc
    if not isinstance(payload, list):
        raise EvalContractError("Companion eval contracts must be a JSON array")
    try:
        contracts = [CompanionEvalContract.model_validate(item) for item in payload]
    except ValueError as exc:
        raise EvalContractError(f"Invalid companion eval contract: {exc}") from exc
    identifiers = [contract.id for contract in contracts]
    if len(identifiers) != len(set(identifiers)):
        raise EvalContractError("Companion eval contract IDs must be unique")
    if not contracts:
        raise EvalContractError("Companion eval contracts cannot be empty")
    return contracts


def load_tasks(path: str | Path | None = None, *, suite: str = "core", task_ids: Iterable[str] | None = None) -> list[EvalTaskSpec]:
    source = Path(path) if path else default_tasks_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalContractError(f"无法读取评测任务：{source}: {exc}") from exc
    if not isinstance(payload, list):
        raise EvalContractError("评测任务文件必须是 JSON 数组")
    try:
        tasks = [EvalTaskSpec.model_validate(item) for item in payload]
    except ValueError as exc:
        raise EvalContractError(f"评测任务合同无效：{exc}") from exc
    identifiers = [task.id for task in tasks]
    if len(identifiers) != len(set(identifiers)):
        raise EvalContractError("评测任务 ID 不能重复")
    selected = [task for task in tasks if suite == "all" or task.suite == suite]
    requested = set(task_ids or [])
    if requested:
        unknown = requested - set(identifiers)
        if unknown:
            raise EvalContractError(f"未知评测任务：{', '.join(sorted(unknown))}")
        selected = [task for task in selected if task.id in requested]
    if not selected:
        raise EvalContractError(f"评测集为空：{suite}")
    return selected
