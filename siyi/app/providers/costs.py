"""Credential-free cost evidence. Missing prices/usage are never free calls.

The existing numeric database columns remain compatibility storage; snapshots
are authoritative for whether a stored number is a complete cost estimate.
Prices are explicit configuration, not a fetched invoice or official tariff.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Iterable
from urllib.parse import urlsplit

from app.config import settings
from app.providers.capabilities import provider_identity

COST_FIELDS = ("cost_status", "estimated_cost_usd", "known_cost_usd", "unknown_cost_requests", "pending_cost_requests")
LEDGER_KEY = "cost_reservations_v1"
MAX_RESERVATIONS = 32
MAX_SNAPSHOT_BYTES = 32 * 1024


def decimal_amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def valid_rates(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict) or not {"input", "output"} <= value.keys():
        return None
    for name in ("input", "output"):
        number = value[name]
        try:
            valid = type(number) in (int, float) and math.isfinite(number) and number >= 0
        except (OverflowError, ValueError):
            valid = False
        if not valid:
            return None
    return {name: float(value[name]) for name in ("input", "output")}


def pricing_snapshot(*, provider: str, base_url: str, model: str) -> dict[str, Any]:
    """Freeze rates, bound to a provider/endpoint/model, without raw URLs."""
    endpoint_hash = provider_identity(base_url)[1]
    scoped_key = f"{provider}:{endpoint_hash}:{model}"
    rates = settings.model_pricing.get(scoped_key)
    source = "explicit_scoped_configuration"
    if rates is None:
        # Historical name-only configuration belongs to its original endpoint,
        # not an arbitrary same-named model at a different gateway/provider.
        configured_host = (urlsplit(settings.model_base_url).hostname or "").casefold()
        expected_provider = "deepseek" if configured_host == "api.deepseek.com" else "openai-compatible"
        allowed_providers = {expected_provider}
        # The original transport labels unregistered providers by endpoint host.
        allowed_providers.add(urlsplit(settings.model_base_url).netloc)
        if expected_provider == "openai-compatible":
            allowed_providers.add("openai_compatible")
        if provider in allowed_providers and endpoint_hash == provider_identity(settings.model_base_url)[1]:
            rates = settings.model_pricing.get(model)
        source = "explicit_legacy_endpoint_configuration"
    # Installed local Ollama is not an API-credit service. This is an explicit
    # billing classification, not an inference from an empty rate dictionary.
    endpoint = urlsplit(base_url)
    if (provider == "ollama" and endpoint.scheme == "http"
            and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
            and endpoint.port == 11434 and not endpoint.username and not endpoint.password
            and not endpoint.query and not endpoint.fragment):
        rates, source = {"input": 0.0, "output": 0.0}, "local_api_unbilled"
    return {"version": 1, "provider": provider, "endpoint_hash": endpoint_hash, "model": model,
            "currency": "USD", "unit": "per_million_tokens", "source": source,
            "rates": dict(rates) if rates is not None else None}


def complete_usage(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, dict):
        return None
    numbers = [value.get(name) for name in ("prompt_tokens", "completion_tokens")]
    if any(type(number) is not int or number < 0 for number in numbers):
        return None
    if "total_tokens" in value and (type(value["total_tokens"]) is not int or value["total_tokens"] != sum(numbers)):
        return None
    return numbers[0], numbers[1]


def cost_decimal(rates: dict[str, float], input_tokens: int, output_tokens: int) -> Decimal:
    return (Decimal(input_tokens) * Decimal(str(rates["input"]))
            + Decimal(output_tokens) * Decimal(str(rates["output"]))) / Decimal(1_000_000)


def reservation_amount(rates: dict[str, float], input_tokens: int, output_tokens: int) -> Decimal:
    # Never round a small positive reservation down to zero.
    return cost_decimal(rates, input_tokens, output_tokens).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)


def usage_snapshot(price: dict[str, Any], usage: Any, *, attempts: int = 1,
                   successful: bool = True, request_sent: bool = True) -> dict[str, Any]:
    result = dict(price)
    rates, tokens = valid_rates(price.get("rates")), complete_usage(usage)
    known = Decimal(0)
    status, reason = "unknown", "pricing_missing"
    if not request_sent:
        status, reason = "known", "not_sent"
    elif rates is not None and tokens is not None and successful:
        known = cost_decimal(rates, *tokens)
        status, reason = ("known", "complete_usage") if attempts == 1 else ("partial", "retry_usage_unknown")
    elif rates is not None:
        reason = "usage_missing_or_incomplete"
    result.update({"cost_status": status, "reason": reason, "usage_complete": tokens is not None,
                   "known_cost_usd_decimal": str(known), "attempts": attempts})
    return result


def decode_snapshot(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        return {}
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def row_cost(row: dict[str, Any]) -> tuple[str, Decimal]:
    snapshot = decode_snapshot(row.get("price_snapshot_json"))
    if snapshot.get("version") == 1:
        status = snapshot.get("cost_status")
        amount = decimal_amount(snapshot.get("known_cost_usd_decimal"))
        identity_ok = (snapshot.get("provider") == row.get("provider")
                       and snapshot.get("model") == row.get("model")
                       and isinstance(snapshot.get("endpoint_hash"), str)
                       and re.fullmatch(r"[0-9a-f]{16}", snapshot["endpoint_hash"]) is not None)
        rates = valid_rates(snapshot.get("rates"))
        tokens = complete_usage({"prompt_tokens": row.get("input_tokens"), "completion_tokens": row.get("output_tokens"),
                                 "total_tokens": row.get("total_tokens")})
        evidence_ok = (snapshot.get("reason") == "not_sent" and amount == 0) or (
            rates is not None and snapshot.get("usage_complete") is True and tokens is not None
            and bool(row.get("success")) and cost_decimal(rates, *tokens) == amount)
        if identity_ok and amount is not None and status in {"known", "partial"} and evidence_ok:
            return status, amount
        return "unknown", Decimal(0)
    # A complete old tariff snapshot is usable only with observable nonzero
    # usage. Empty historical snapshots and absent usage stay unknown.
    rates = valid_rates(snapshot)
    tokens = complete_usage({"prompt_tokens": row.get("input_tokens"), "completion_tokens": row.get("output_tokens"),
                             "total_tokens": row.get("total_tokens")})
    stored = decimal_amount(row.get("estimated_cost_usd"))
    if rates is not None and tokens is not None and sum(tokens) > 0 and stored is not None:
        # Legacy tariffs were not endpoint-bound and cannot prove complete cost.
        return "partial", stored
    return "unknown", Decimal(0)


@dataclass
class CostAccumulator:
    known: Decimal = Decimal(0)
    known_count: int = 0
    unknown_count: int = 0

    def add(self, record: dict[str, Any]) -> None:
        status, amount = row_cost(record)
        self.known += amount
        self.known_count += status in {"known", "partial"}
        self.unknown_count += status != "known"

    def fields(self, *, pending: int = 0) -> dict[str, Any]:
        uncertain = self.unknown_count > 0 or pending > 0
        status = ("partial" if self.known_count else "unknown") if uncertain else "known"
        return {"cost_status": status, "estimated_cost_usd": None if uncertain else float(self.known),
                "known_cost_usd": float(self.known), "unknown_cost_requests": self.unknown_count,
                "pending_cost_requests": pending}


def cost_summary(records: Iterable[dict[str, Any]], *, pending: int = 0) -> dict[str, Any]:
    accumulator = CostAccumulator()
    for record in records:
        accumulator.add(record)
    return accumulator.fields(pending=pending)


def public_model_run(record: dict[str, Any]) -> dict[str, Any]:
    return {**{key: value for key, value in record.items() if key != "price_snapshot_json"}, **cost_summary([record])}


def pending_reservations(task: dict[str, Any]) -> int:
    raw = task.get("price_snapshot_json", "{}")
    ledger = decode_snapshot(raw)
    if not ledger and raw not in ("", "{}", None):
        return 1
    reservations = ledger.get(LEDGER_KEY, {})
    if not isinstance(reservations, dict) or len(reservations) > MAX_RESERVATIONS:
        return 1
    for item in reservations.values():
        if (not isinstance(item, dict) or type(item.get("generation")) is not int
                or decimal_amount(item.get("upper_bound_usd")) is None
                or item["generation"] != task.get("lease_generation")):
            return max(1, len(reservations))
    return len(reservations)


def task_cost_fields(task: dict[str, Any], *, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if records is None:
        from app.database import rows
        records = rows("SELECT provider,model,input_tokens,output_tokens,total_tokens,retry_count,success,estimated_cost_usd,price_snapshot_json FROM model_runs WHERE task_id=?", (task["id"],))
    result = cost_summary(records, pending=pending_reservations(task))
    if not records and (int(task.get("total_tokens") or 0) > 0 or float(task.get("estimated_cost_usd") or 0) > 0):
        result.update(cost_status="unknown", estimated_cost_usd=None, known_cost_usd=0.0, unknown_cost_requests=1)
    return result


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {**{key: value for key, value in task.items() if key != "price_snapshot_json"}, **task_cost_fields(task)}
