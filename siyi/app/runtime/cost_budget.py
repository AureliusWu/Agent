"""Atomic estimate reservations for explicitly dollar-bounded root tasks.

No tariff injection, no schema change, no TTL recovery of uncertain charges.
Input-token preflight is an estimate, not an actual billing guarantee.
"""
from __future__ import annotations

import json
import hashlib
import os
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.database import connect
from app.config import settings
from app.providers.costs import (LEDGER_KEY, MAX_RESERVATIONS, MAX_SNAPSHOT_BYTES, cost_summary, decimal_amount,
                                 decode_snapshot, reservation_amount, valid_rates)
from app.runtime.task_leases import current_task_lease, require_current_task_lease

COST_ERRORS = frozenset({"cost_pricing_unknown", "cost_usage_unknown", "cost_budget_limit", "cost_lease_required", "cost_input_unknown"})
# A settlement failure can coincide with a database outage. Keep a shared,
# process-lifetime fuse independent of ContextVar copies and lease renewal.
# On restart the durable reservation still fences the next lease generation.
MAX_UNCERTAIN_LEASES = 4096
_uncertain_leases: set[tuple[str, str, int]] = set()
_uncertain_lock = threading.Lock()
_uncertain_capacity_exhausted = False


class CostBudgetBlocked(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _lease_key(task_id: str, generation: int) -> tuple[str, str, int]:
    database_identity = os.path.normcase(str(settings.database_path.resolve()))
    return hashlib.sha256(database_identity.encode("utf-8")).hexdigest(), task_id, generation


def _trip_lease(task_id: str, generation: int) -> None:
    global _uncertain_capacity_exhausted
    key = _lease_key(task_id, generation)
    with _uncertain_lock:
        if key in _uncertain_leases:
            return
        if len(_uncertain_leases) >= MAX_UNCERTAIN_LEASES:
            _uncertain_capacity_exhausted = True
        else:
            _uncertain_leases.add(key)


def _require_unfused_lease(lease: Any, *, check_capacity: bool = False) -> None:
    if lease is None:
        return
    key = _lease_key(lease.task_id, lease.generation)
    with _uncertain_lock:
        blocked = key in _uncertain_leases or (check_capacity and _uncertain_capacity_exhausted)
    if blocked:
        raise CostBudgetBlocked("cost_usage_unknown", "根任务租约存在未确认费用结算，已停止同代模型调用与工具执行。")


@dataclass(frozen=True)
class CostReservation:
    task_id: str
    id: str
    generation: int
    upper_bound: Decimal


def _dollar_budget(task: Any) -> Decimal | None:
    raw = task["cost_budget_limit"] if task is not None else None
    if raw is None:
        return None
    budget = decimal_amount(raw)
    if budget is None or budget <= 0:
        raise CostBudgetBlocked("cost_usage_unknown", "任务美元预算无效，已停止模型调用。")
    return budget


def reserve(*, task_id: str | None, price: dict[str, Any], input_tokens: int,
            output_tokens: int, unsupported_input: bool = False) -> CostReservation | None:
    lease = current_task_lease()
    _require_unfused_lease(lease)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # A call may not detach from a dollar-bounded active parent by passing
        # None or an arbitrary task ID (including background title calls).
        parent = db.execute("SELECT * FROM agent_tasks WHERE id=?", (lease.task_id,)).fetchone() if lease else None
        if _dollar_budget(parent) is not None and (not task_id or task_id != lease.task_id):
            raise CostBudgetBlocked("cost_lease_required", "模型调用未绑定当前成本受限任务，已停止。")
        task = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone() if task_id else None
        budget = _dollar_budget(task)
        if budget is None:
            return None
        if lease is None or lease.task_id != task_id:
            raise CostBudgetBlocked("cost_lease_required", "美元预算调用必须持有当前根任务租约。")
        _require_unfused_lease(lease, check_capacity=True)
        require_current_task_lease(lease, db=db)
        if "price_snapshot_json" not in task.keys():
            raise CostBudgetBlocked("cost_usage_unknown", "历史数据库缺少费用预留字段；已安全拒绝美元预算调用。")
        rates = valid_rates(price.get("rates"))
        if rates is None:
            raise CostBudgetBlocked("cost_pricing_unknown", "当前端点和模型未配置有效单价，不能执行有美元预算的调用。")
        if unsupported_input:
            raise CostBudgetBlocked("cost_input_unknown", "当前多模态输入的计费 Token 上界未确认，不能执行有美元预算的调用。")
        ledger = decode_snapshot(task["price_snapshot_json"])
        if task["price_snapshot_json"] not in ("", "{}") and not ledger:
            raise CostBudgetBlocked("cost_usage_unknown", "任务费用预留记录无效，不能继续付费调用。")
        reservations = ledger.get(LEDGER_KEY, {})
        if not isinstance(reservations, dict) or len(reservations) >= MAX_RESERVATIONS:
            raise CostBudgetBlocked("cost_usage_unknown", "任务费用预留记录超限或无效。")
        reserved = Decimal(0)
        for item in reservations.values():
            amount = decimal_amount(item.get("upper_bound_usd")) if isinstance(item, dict) else None
            if amount is None or type(item.get("generation")) is not int or item.get("generation") != lease.generation or item.get("state") == "uncertain":
                raise CostBudgetBlocked("cost_usage_unknown", "历史模型调用费用尚未确认，不能自动释放或继续付费调用。")
            reserved += amount
        records = [dict(row) for row in db.execute("SELECT * FROM model_runs WHERE task_id=?", (task_id,))]
        summary = cost_summary(records)
        if summary["unknown_cost_requests"] or (not records and (task["total_tokens"] > 0 or task["estimated_cost_usd"] > 0)):
            raise CostBudgetBlocked("cost_usage_unknown", "该任务已有无法确认的费用，不能按零费用继续执行。")
        # Use decimal evidence, not binary floating-point totals, to compare.
        from app.providers.costs import row_cost
        known = sum((row_cost(record)[1] for record in records), Decimal(0))
        if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
            raise CostBudgetBlocked("cost_input_unknown", "请求 Token 估值无效，已停止模型调用。")
        upper = reservation_amount(rates, input_tokens, output_tokens)
        if known + reserved + upper > budget:
            raise CostBudgetBlocked("cost_budget_limit", "已知费用与本次请求费用预留超过任务美元预算，已在调用前停止。")
        reservation = CostReservation(str(task_id), uuid.uuid4().hex, lease.generation, upper)
        reservations[reservation.id] = {"generation": lease.generation, "upper_bound_usd": str(upper),
                                        "provider": price["provider"], "endpoint_hash": price["endpoint_hash"], "model": price["model"]}
        ledger[LEDGER_KEY] = reservations
        encoded = json.dumps(ledger, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise CostBudgetBlocked("cost_usage_unknown", "任务费用预留记录超限，已拒绝新增调用。")
        db.execute("UPDATE agent_tasks SET price_snapshot_json=? WHERE id=?", (encoded, task_id))
        return reservation


def settle_in_transaction(db, reservation: CostReservation) -> None:
    lease = current_task_lease(reservation.task_id)
    if lease is None or lease.generation != reservation.generation:
        raise CostBudgetBlocked("cost_lease_required", "任务费用结算租约已变化；预留保持未确认。")
    require_current_task_lease(lease, db=db)
    task = db.execute("SELECT price_snapshot_json FROM agent_tasks WHERE id=?", (reservation.task_id,)).fetchone()
    ledger = decode_snapshot(task["price_snapshot_json"] if task else None)
    reservations = ledger.get(LEDGER_KEY)
    if not isinstance(reservations, dict) or reservation.id not in reservations:
        raise CostBudgetBlocked("cost_usage_unknown", "任务费用结算缺少对应预留，已拒绝重置账本。")
    item = reservations[reservation.id]
    if (not isinstance(item, dict) or type(item.get("generation")) is not int
            or item["generation"] != reservation.generation
            or decimal_amount(item.get("upper_bound_usd")) != reservation.upper_bound):
        raise CostBudgetBlocked("cost_usage_unknown", "任务费用预留证据已变化，已拒绝重置账本。")
    del reservations[reservation.id]
    db.execute("UPDATE agent_tasks SET price_snapshot_json=? WHERE id=?", (json.dumps(ledger, allow_nan=False), reservation.task_id))


def release_unsent(reservation: CostReservation | None) -> None:
    if reservation is not None:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            settle_in_transaction(db, reservation)


def mark_uncertain(reservation: CostReservation | None) -> None:
    """Best effort only: a failed settlement must never enable same-lease retry."""
    if reservation is None:
        return
    # Trip before any database work: even a failed durable mark must stop a
    # sibling/caller that catches the error and reuses this lease generation.
    _trip_lease(reservation.task_id, reservation.generation)
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = current_task_lease(reservation.task_id)
            if lease is None or lease.generation != reservation.generation:
                return
            require_current_task_lease(lease, db=db)
            row = db.execute("SELECT price_snapshot_json FROM agent_tasks WHERE id=?", (reservation.task_id,)).fetchone()
            ledger = decode_snapshot(row["price_snapshot_json"] if row else None)
            items = ledger.get(LEDGER_KEY)
            if isinstance(items, dict) and isinstance(items.get(reservation.id), dict):
                items[reservation.id]["state"] = "uncertain"
                encoded = json.dumps(ledger, allow_nan=False)
                if len(encoded.encode("utf-8")) <= MAX_SNAPSHOT_BYTES:
                    db.execute("UPDATE agent_tasks SET price_snapshot_json=? WHERE id=?", (encoded, reservation.task_id))
    except Exception:
        # Persistence unavailable: retain the original reservation. The caller
        # propagates a fatal cost error; a new generation cannot replay it.
        pass


def check_result(task_id: str, metrics: dict[str, Any]) -> None:
    """Do not execute tools from a response with unconfirmed bounded cost."""
    if metrics.get("cost_status") != "known":
        raise CostBudgetBlocked("cost_usage_unknown", "本次模型调用费用无法确认，已停止后续工具和模型执行。")
    _require_unfused_lease(current_task_lease(task_id), check_capacity=True)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        lease = current_task_lease(task_id)
        if lease is None:
            raise CostBudgetBlocked("cost_lease_required", "费用结算后根任务租约缺失，已停止执行。")
        require_current_task_lease(lease, db=db)
        task = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        budget = _dollar_budget(task)
        records = [dict(row) for row in db.execute("SELECT * FROM model_runs WHERE task_id=?", (task_id,))]
        if cost_summary(records)["unknown_cost_requests"]:
            raise CostBudgetBlocked("cost_usage_unknown", "当前任务其他模型调用已有未知费用，已停止后续执行。")
        raw = task["price_snapshot_json"]
        ledger = decode_snapshot(raw)
        reservations = ledger.get(LEDGER_KEY, {})
        if (not ledger and raw not in ("", "{}")) or not isinstance(reservations, dict) or len(reservations) > MAX_RESERVATIONS:
            raise CostBudgetBlocked("cost_usage_unknown", "当前任务费用预留账本无效，已停止执行。")
        reserved = Decimal(0)
        for item in reservations.values():
            amount = decimal_amount(item.get("upper_bound_usd")) if isinstance(item, dict) else None
            if amount is None or type(item.get("generation")) is not int or item["generation"] != lease.generation or item.get("state") == "uncertain":
                raise CostBudgetBlocked("cost_usage_unknown", "当前任务存在未确认历史费用预留，已停止执行。")
            reserved += amount
        from app.providers.costs import row_cost
        known = sum((row_cost(row)[1] for row in records), Decimal(0))
        if budget is None or known + reserved > budget:
            raise CostBudgetBlocked("cost_budget_limit", "实际已知费用已超过任务美元预算，已停止后续工具和模型执行。")
