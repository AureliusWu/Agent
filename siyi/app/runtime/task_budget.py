from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


TokenBudgetMode = Literal["soft", "hard"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_deadline(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("task_deadline_at 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class TaskBudgetContract:
    """Durable task-wide limits, separate from one execution segment.

    ``segment_timeout_seconds`` bounds one model/execution segment and may roll
    over into a fresh segment.  ``task_deadline_at`` is an absolute wall-clock
    hard stop.  Token and cost limits account for model usage and are persisted
    independently so that resume cannot silently reset either contract.
    """

    segment_timeout_seconds: float
    task_deadline_at: str | None
    token_budget_limit: int
    token_budget_mode: TokenBudgetMode
    cost_budget_limit: float | None

    def as_task_fields(self) -> dict[str, Any]:
        return {
            "segment_timeout_seconds": self.segment_timeout_seconds,
            "task_deadline_at": self.task_deadline_at,
            "token_budget_limit": self.token_budget_limit,
            "token_budget_mode": self.token_budget_mode,
            "cost_budget_limit": self.cost_budget_limit,
        }

    def deadline_remaining_seconds(self, *, now: datetime | None = None) -> float | None:
        if not self.task_deadline_at:
            return None
        deadline = datetime.fromisoformat(self.task_deadline_at)
        current = now or _utc_now()
        return (deadline - current.astimezone(timezone.utc)).total_seconds()

    def wait_timeout(self, segment_remaining_seconds: float) -> tuple[float, str]:
        """Return the next bounded wait and the limit responsible for it."""

        segment_remaining = max(float(segment_remaining_seconds), 0.001)
        deadline_remaining = self.deadline_remaining_seconds()
        if deadline_remaining is None:
            return segment_remaining, "segment_timeout"
        if deadline_remaining <= 0:
            return 0.0, "task_deadline"
        if deadline_remaining <= segment_remaining:
            return max(deadline_remaining, 0.001), "task_deadline"
        return segment_remaining, "segment_timeout"

    def cost_budget_reason(self, estimated_cost_usd: float, *, before_call: bool = False) -> str | None:
        if self.cost_budget_limit is None:
            return None
        used = max(float(estimated_cost_usd), 0.0)
        exhausted = used >= self.cost_budget_limit if before_call else used > self.cost_budget_limit
        if not exhausted:
            return None
        return (
            f"任务成本预算已用 ${used:.8f}/${self.cost_budget_limit:.8f}，"
            + ("已在下一次模型调用前停止" if before_call else "已停止后续模型调用")
        )


def new_task_budget_contract(payload: Any, runtime_limits: Any) -> TaskBudgetContract:
    legacy_limit = getattr(payload, "budget_limit", None)
    explicit_limit = getattr(payload, "token_budget_limit", None)
    requested_limit = explicit_limit if explicit_limit is not None else legacy_limit
    token_limit = max(1, min(int(requested_limit or runtime_limits.max_task_tokens), int(runtime_limits.max_task_tokens)))
    requested_mode = getattr(payload, "token_budget_mode", None)
    token_mode: TokenBudgetMode = requested_mode or ("hard" if requested_limit is not None else "soft")
    segment_timeout = getattr(payload, "segment_timeout_seconds", None)
    segment_timeout = float(segment_timeout or runtime_limits.task_timeout_seconds)
    cost_limit = getattr(payload, "cost_budget_limit", None)
    return TaskBudgetContract(
        segment_timeout_seconds=max(segment_timeout, 0.001),
        task_deadline_at=_normalize_deadline(getattr(payload, "task_deadline_at", None)),
        token_budget_limit=token_limit,
        token_budget_mode=token_mode,
        cost_budget_limit=float(cost_limit) if cost_limit is not None else None,
    )


def persisted_task_budget_contract(record: dict[str, Any], runtime_limits: Any) -> TaskBudgetContract:
    stored_limit = int(record.get("token_budget_limit") or 0)
    mode = str(record.get("token_budget_mode") or "soft")
    if mode not in {"soft", "hard"}:
        mode = "soft"
    segment_timeout = float(record.get("segment_timeout_seconds") or 0)
    cost_limit = record.get("cost_budget_limit")
    return TaskBudgetContract(
        segment_timeout_seconds=segment_timeout if segment_timeout > 0 else float(runtime_limits.task_timeout_seconds),
        task_deadline_at=_normalize_deadline(record.get("task_deadline_at")),
        token_budget_limit=stored_limit if stored_limit > 0 else int(runtime_limits.max_task_tokens),
        token_budget_mode=mode,  # type: ignore[arg-type]
        cost_budget_limit=float(cost_limit) if cost_limit is not None else None,
    )
