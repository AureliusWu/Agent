"""Synthetic cost evidence: absent data never becomes a free request."""
import json
from decimal import Decimal

import pytest

from app.config import settings
from app.providers.costs import (cost_summary, pricing_snapshot, usage_snapshot, row_cost,
                                 valid_rates, complete_usage, reservation_amount, pending_reservations)
from app.providers.capabilities import provider_identity


def _row(rates=None, usage=None, **changes):
    rates = {"input": 1.0, "output": 2.0} if rates is None else rates
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15} if usage is None else usage
    price = {"version": 1, "provider": "deepseek", "endpoint_hash": "a" * 16, "model": "synthetic", "rates": rates}
    snapshot = usage_snapshot(price, usage)
    row = {"provider": "deepseek", "model": "synthetic", "success": 1, "input_tokens": usage.get("prompt_tokens", 0),
           "output_tokens": usage.get("completion_tokens", 0), "total_tokens": usage.get("total_tokens", 0),
           "retry_count": 0, "estimated_cost_usd": float(snapshot["known_cost_usd_decimal"]),
           "price_snapshot_json": json.dumps(snapshot)}
    row.update(changes)
    return row


@pytest.mark.parametrize("rates", [{}, {"input": 1}, {"output": 1}, {"input": -1, "output": 0},
    {"input": True, "output": 0}, {"input": "1", "output": 2}, {"input": float("nan"), "output": 2},
    {"input": 1, "output": float("inf")}, {"input": 10 ** 400, "output": 2}])
def test_invalid_prices_are_unknown_in_configuration_and_evidence(monkeypatch, rates):
    monkeypatch.setattr(settings, "model_pricing_json", json.dumps({"synthetic": rates}))
    assert settings.model_pricing == {}
    assert valid_rates(rates) is None


@pytest.mark.parametrize("usage", [{}, {"prompt_tokens": 4}, {"prompt_tokens": 4, "completion_tokens": False},
    {"prompt_tokens": -1, "completion_tokens": 0}, {"prompt_tokens": "4", "completion_tokens": 0},
    {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 6}])
def test_missing_or_invalid_usage_never_known_even_with_prices(usage):
    assert complete_usage(usage) is None
    assert cost_summary([_row(usage=usage)])["estimated_cost_usd"] is None


def test_explicit_free_and_missing_price_are_distinct():
    free = cost_summary([_row(rates={"input": 0, "output": 0})])
    unknown = cost_summary([_row(rates={})])
    assert free["cost_status"] == "known" and free["estimated_cost_usd"] == 0
    assert unknown["cost_status"] == "unknown" and unknown["estimated_cost_usd"] is None
    assert unknown["unknown_cost_requests"] == 1


def test_partial_aggregate_retains_known_subtotal_without_fake_total():
    summary = cost_summary([_row(), _row(rates={})])
    assert summary["cost_status"] == "partial"
    assert summary["estimated_cost_usd"] is None
    assert summary["known_cost_usd"] == 0.00002
    assert summary["unknown_cost_requests"] == 1


def test_old_unbound_tariff_is_partial_and_empty_snapshot_unknown():
    row = _row(price_snapshot_json='{"input":1,"output":2}')
    assert row_cost(row) == ("partial", Decimal("0.00002"))
    row["price_snapshot_json"] = "{}"
    assert row_cost(row) == ("unknown", Decimal(0))


@pytest.mark.parametrize("change", [{"success": 0}, {"total_tokens": 99}, {"provider": "other"}, {"model": "other"},
    {"input_tokens": False}])
def test_snapshot_requires_matching_identity_complete_actual_usage_and_success(change):
    assert row_cost(_row(**change))[0] == "unknown"


def test_snapshot_amount_cannot_disagree_with_tokens():
    row = _row()
    snapshot = json.loads(row["price_snapshot_json"])
    snapshot["known_cost_usd_decimal"] = "0"
    row["price_snapshot_json"] = json.dumps(snapshot)
    assert row_cost(row)[0] == "unknown"


def test_final_success_after_retry_retains_unconfirmed_prior_usage():
    row = _row()
    snapshot = json.loads(row["price_snapshot_json"])
    snapshot.update(cost_status="partial", reason="retry_usage_unknown", attempts=2)
    row["price_snapshot_json"] = json.dumps(snapshot)
    assert cost_summary([row])["estimated_cost_usd"] is None


def test_price_endpoint_binding_and_frozen_rates(monkeypatch):
    monkeypatch.setattr(settings, "model_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(settings, "model_pricing_json", '{"synthetic":{"input":1,"output":2}}')
    snapshot = pricing_snapshot(provider="api.deepseek.com", base_url=settings.model_base_url, model="synthetic")
    assert snapshot["rates"] == {"input": 1, "output": 2}
    monkeypatch.setattr(settings, "model_pricing_json", '{"synthetic":{"input":999,"output":999}}')
    assert snapshot["rates"] == {"input": 1, "output": 2}
    assert pricing_snapshot(provider="deepseek", base_url="https://gateway.example/v1", model="synthetic")["rates"] is None
    assert pricing_snapshot(provider="openai_compatible", base_url=settings.model_base_url, model="synthetic")["rates"] is None


def test_explicit_scoped_gateway_price_is_supported(monkeypatch):
    endpoint = "https://gateway.example/v1"
    key = f"openai_compatible:{provider_identity(endpoint)[1]}:synthetic"
    monkeypatch.setattr(settings, "model_pricing_json", json.dumps({key: {"input": 3, "output": 4}}))
    assert pricing_snapshot(provider="openai_compatible", base_url=endpoint, model="synthetic")["rates"] == {"input": 3, "output": 4}


@pytest.mark.parametrize("endpoint", ["https://remote.example/v1", "https://127.0.0.1:11434/v1", "http://127.0.0.1:1234/v1"])
def test_ollama_identity_does_not_make_arbitrary_remote_endpoint_free(monkeypatch, endpoint):
    monkeypatch.setattr(settings, "model_pricing_json", "{}")
    assert pricing_snapshot(provider="ollama", base_url=endpoint, model="synthetic")["rates"] is None


def test_local_ollama_is_explicitly_unbilled_and_tiny_positive_reservation_never_rounds_to_zero():
    snapshot = pricing_snapshot(provider="ollama", base_url="http://127.0.0.1:11434/v1", model="synthetic")
    assert snapshot["rates"] == {"input": 0, "output": 0}
    assert reservation_amount({"input": 0.000001, "output": 0}, 1, 0) == Decimal("0.00000001")


@pytest.mark.parametrize("raw", ["broken", "[]", "x" * 33000, '{"cost_reservations_v1":[]}',
    '{"cost_reservations_v1":{"x":{"generation":true,"upper_bound_usd":"0"}}}'],
    ids=["invalid_json", "array", "oversized", "bad_map", "bool_generation"])
def test_invalid_task_ledger_is_never_empty_known_zero(raw):
    assert pending_reservations({"price_snapshot_json": raw, "lease_generation": 1}) > 0


def test_cost_gate_failures_do_not_penalize_model_quality():
    from app.providers.base import FailureCategory
    from app.providers.registry import failure_category
    assert failure_category("cost_pricing_unknown") == FailureCategory.ENVIRONMENT_FAILURE
    assert failure_category("cost_usage_unknown") == FailureCategory.ENVIRONMENT_FAILURE
    assert failure_category("cost_budget_limit") == FailureCategory.RUNTIME_FAILURE
