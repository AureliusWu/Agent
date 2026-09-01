"""Pure restart decisions; persistence and Executor ownership stay in Runner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from app.runtime.recovery import MUTATION_TOOLS
from app.runtime.task_state import TaskStatus


PROVIDER_WAIT_ERRORS = {
    "missing_api_key", "authentication", "rate_limited", "quota_exhausted",
    "server_error", "timeout", "network_error", "retry_exhausted",
}


def provider_wait_status(error_type: str) -> TaskStatus:
    if error_type in {"missing_api_key", "authentication"}:
        return TaskStatus.WAITING_PROVIDER_CREDENTIAL
    if error_type in PROVIDER_WAIT_ERRORS:
        return TaskStatus.WAITING_PROVIDER
    return TaskStatus.INTERRUPTED


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class OperationRecovery:
    result: dict[str, Any] | None = None
    uncertain: bool = False


def recover_tool_operation(
    operation: dict[str, Any],
    *,
    canonical_name: str,
    side_effect: bool,
    retry_uncertain: bool,
    recover_mutation: Callable[[], dict[str, Any] | None],
    restart: Callable[[str], Any],
    set_status: Callable[..., Any],
) -> OperationRecovery:
    """Reuse receipts and never authorize an unknown side effect for replay."""
    if operation["created"]:
        return OperationRecovery()
    status = operation["status"]
    execution_id = str(operation["execution_id"])
    if status in {"completed", "failed"}:
        return OperationRecovery(operation.get("result") or {
            "success": status == "completed", "status": "ok" if status == "completed" else "error",
        })
    if status in {"running", "uncertain"}:
        result = recover_mutation() if canonical_name in MUTATION_TOOLS else None
        if result is not None:
            return OperationRecovery(result)
        if not side_effect or retry_uncertain:
            restart(execution_id)
        else:
            set_status(execution_id, "uncertain", operation.get("result"))
            return OperationRecovery(uncertain=True)
    elif status in {"waiting_confirmation", "cancelled"}:
        restart(execution_id)
    return OperationRecovery()
