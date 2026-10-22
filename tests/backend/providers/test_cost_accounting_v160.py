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


def _failed_row(*, usage=None, rates=None, attempts=1, confirmed=True):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15} if usage is None else usage
    row = _row(rates=rates, usage=usage)
    snapshot = usage_snapshot(json.loads(row["price_snapshot_json"]), usage, attempts=attempts,
                              successful=False, response_usage_confirmed=confirmed)
    tokens = complete_usage(usage)
    row.update(success=0, retry_count=attempts - 1,
               total_tokens=sum(tokens) if tokens is not None else row["total_tokens"],
               estimated_cost_usd=float(snapshot["known_cost_usd_decimal"]), price_snapshot_json=json.dumps(snapshot))
    return row


def test_failed_complete_response_usage_is_cost_evidence_not_model_success():
    row = _failed_row()
    snapshot = json.loads(row["price_snapshot_json"])
    assert row["success"] == 0
    assert snapshot["version"] == 1
    assert snapshot["response_usage_confirmed"] is True
    assert snapshot["reason"] == "complete_response_usage"
    assert snapshot["cost_status"] == "known"
    assert row_cost(row) == ("known", Decimal("0.00002"))
    assert cost_summary([row])["estimated_cost_usd"] == 0.00002


def test_failed_response_confirmation_defaults_off_and_does_not_reprice_old_failure():
    price = {"version": 1, "provider": "deepseek", "endpoint_hash": "a" * 16,
             "model": "synthetic", "rates": {"input": 1, "output": 2}}
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    snapshot = usage_snapshot(price, usage, successful=False)
    assert snapshot["response_usage_confirmed"] is False
    assert snapshot["cost_status"] == "unknown"
    assert snapshot["known_cost_usd_decimal"] == "0"
    assert row_cost(_failed_row(confirmed=False)) == ("unknown", Decimal(0))
    legacy = _row(success=0)
    legacy_snapshot = json.loads(legacy["price_snapshot_json"])
    legacy_snapshot.pop("response_usage_confirmed")
    legacy["price_snapshot_json"] = json.dumps(legacy_snapshot)
    assert row_cost(legacy) == ("unknown", Decimal(0))


@pytest.mark.parametrize("confirmed", [False, None, 0, 1, "true", "false", [], {}])
def test_failed_usage_requires_literal_true_transport_confirmation(confirmed):
    row = _failed_row(confirmed=confirmed)
    snapshot = json.loads(row["price_snapshot_json"])
    assert snapshot["response_usage_confirmed"] is False
    assert row_cost(row) == ("unknown", Decimal(0))


@pytest.mark.parametrize("usage", [
    {}, {"prompt_tokens": 4}, {"completion_tokens": 1},
    {"prompt_tokens": 4, "completion_tokens": False},
    {"prompt_tokens": True, "completion_tokens": 1},
    {"prompt_tokens": -1, "completion_tokens": 0},
    {"prompt_tokens": "4", "completion_tokens": 0},
    {"prompt_tokens": 4.0, "completion_tokens": 0},
    {"prompt_tokens": float("nan"), "completion_tokens": 0},
    {"prompt_tokens": 4, "completion_tokens": float("inf")},
    {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 6},
    {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5.0},
    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": False},
])
def test_transport_confirmation_cannot_repair_missing_or_invalid_raw_usage(usage):
    row = _failed_row(usage=usage)
    snapshot = json.loads(row["price_snapshot_json"])
    assert snapshot["response_usage_confirmed"] is True
    assert snapshot["usage_complete"] is False
    assert snapshot["cost_status"] == "unknown"
    assert row_cost(row) == ("unknown", Decimal(0))


@pytest.mark.parametrize("rates", [{}, {"input": 1}, {"input": -1, "output": 2},
                                   {"input": True, "output": 2}, {"input": 1, "output": float("inf")}])
def test_failed_complete_usage_still_requires_valid_endpoint_price(rates):
    row = _failed_row(rates=rates)
    assert json.loads(row["price_snapshot_json"])["cost_status"] == "unknown"
    assert row_cost(row) == ("unknown", Decimal(0))


def test_failed_zero_usage_and_optional_total_are_independently_validated():
    zero = _failed_row(usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    assert row_cost(zero) == ("known", Decimal(0))
    no_total = _failed_row(usage={"prompt_tokens": 10, "completion_tokens": 5})
    assert no_total["total_tokens"] == 15
    assert row_cost(no_total) == ("known", Decimal("0.00002"))


@pytest.mark.parametrize("attempts", [2, 3])
def test_failed_complete_last_attempt_retains_only_known_subtotal(attempts):
    row = _failed_row(attempts=attempts)
    snapshot = json.loads(row["price_snapshot_json"])
    assert snapshot["reason"] == "retry_response_usage_unknown"
    assert row_cost(row) == ("partial", Decimal("0.00002"))
    summary = cost_summary([row])
    assert summary["cost_status"] == "partial"
    assert summary["estimated_cost_usd"] is None
    assert summary["known_cost_usd"] == 0.00002
    assert summary["unknown_cost_requests"] == 1


@pytest.mark.parametrize("attempts", [True, False, 0, -1, 1.0, "1", None])
def test_failed_snapshot_rejects_invalid_attempt_count_before_costing(attempts):
    price = {"version": 1, "rates": {"input": 1, "output": 2}}
    snapshot = usage_snapshot(price, {"prompt_tokens": 10, "completion_tokens": 5}, attempts=attempts,
                              successful=False, response_usage_confirmed=True)
    assert snapshot["cost_status"] == "unknown"
    assert snapshot["known_cost_usd_decimal"] == "0"


@pytest.mark.parametrize("snapshot_changes,row_changes", [
    ({"response_usage_confirmed": False}, {}),
    ({"response_usage_confirmed": 1}, {}),
    ({"response_usage_confirmed": "true"}, {}),
    ({"response_usage_confirmed": None}, {}),
    ({"attempts": True}, {}), ({"attempts": 1.0}, {}), ({"attempts": "1"}, {}),
    ({"attempts": 0}, {}), ({"attempts": -1}, {}), ({"attempts": None}, {}),
    ({}, {"retry_count": True}), ({}, {"retry_count": False}), ({}, {"retry_count": 0.0}),
    ({}, {"retry_count": "0"}), ({}, {"retry_count": -1}), ({}, {"retry_count": None}),
    ({"attempts": 2}, {}), ({}, {"retry_count": 1}),
    ({"attempts": 2}, {"retry_count": 1}),
    ({"cost_status": "partial", "reason": "retry_response_usage_unknown"}, {}),
    ({"reason": "complete_usage"}, {}), ({"reason": "retry_usage_unknown"}, {}),
    ({"usage_complete": False}, {}), ({"usage_complete": 1}, {}),
    ({"version": True}, {}), ({"version": 2}, {}),
    ({"known_cost_usd_decimal": "0"}, {}), ({"known_cost_usd_decimal": "NaN"}, {}),
    ({"provider": "other"}, {}), ({"model": "other"}, {}),
    ({"endpoint_hash": "invalid"}, {}), ({"rates": None}, {}),
    ({}, {"success": None}), ({}, {"success": 0.0}),
    ({}, {"input_tokens": False}), ({}, {"output_tokens": 5.0}), ({}, {"total_tokens": 99}),
])
def test_failed_proof_requires_matching_v1_identity_usage_amount_and_attempt_contract(snapshot_changes, row_changes):
    row = _failed_row()
    snapshot = json.loads(row["price_snapshot_json"])
    snapshot.update(snapshot_changes)
    row.update(row_changes, price_snapshot_json=json.dumps(snapshot))
    assert row_cost(row) == ("unknown", Decimal(0))


def test_failed_proof_cannot_be_removed_from_new_snapshot_or_upgrade_retry_to_known():
    row = _failed_row()
    snapshot = json.loads(row["price_snapshot_json"])
    snapshot.pop("response_usage_confirmed")
    row["price_snapshot_json"] = json.dumps(snapshot)
    assert row_cost(row) == ("unknown", Decimal(0))
    row = _failed_row(attempts=2)
    snapshot = json.loads(row["price_snapshot_json"])
    snapshot.update(cost_status="known", reason="complete_response_usage")
    row["price_snapshot_json"] = json.dumps(snapshot)
    assert row_cost(row) == ("unknown", Decimal(0))


def test_unsent_failure_retains_existing_known_zero_without_response_proof():
    row = _failed_row()
    price = json.loads(row["price_snapshot_json"])
    snapshot = usage_snapshot(price, {}, successful=False, request_sent=False)
    row.update(input_tokens=0, output_tokens=0, total_tokens=0, estimated_cost_usd=0,
               price_snapshot_json=json.dumps(snapshot))
    assert snapshot["reason"] == "not_sent"
    assert snapshot["response_usage_confirmed"] is False
    assert row_cost(row) == ("known", Decimal(0))


def test_failed_cost_snapshot_never_copies_response_usage_payload():
    row = _failed_row(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                             "reasoning_content": "PRIVATE_REASONING_SENTINEL", "api_key": "PRIVATE_KEY_SENTINEL"})
    assert row_cost(row) == ("known", Decimal("0.00002"))
    assert "PRIVATE_REASONING_SENTINEL" not in row["price_snapshot_json"]
    assert "PRIVATE_KEY_SENTINEL" not in row["price_snapshot_json"]


@pytest.mark.parametrize("success", [True, 1])
def test_v1_success_evidence_accepts_only_canonical_true_or_one(success):
    assert row_cost(_row(success=success)) == ("known", Decimal("0.00002"))


@pytest.mark.parametrize("success", [False, 0, -1, 2, 0.0, 1.0, "0", "1", "true", "false", "", None, [], {}, [1], {"success": 1}])
def test_v1_success_evidence_rejects_noncanonical_or_truthy_success(success):
    assert row_cost(_row(success=success)) == ("unknown", Decimal(0))


@pytest.mark.parametrize("success", [True, 1])
@pytest.mark.parametrize("attempts", [1, 2])
@pytest.mark.parametrize("proof_state", ["missing", "false", "literal_true", "truthy_integer", "invalid_attempts"])
def test_failed_response_reason_cannot_borrow_success_evidence(success, attempts, proof_state):
    row = _failed_row(attempts=attempts)
    snapshot = json.loads(row["price_snapshot_json"])
    if proof_state == "missing":
        snapshot.pop("response_usage_confirmed")
    elif proof_state == "false":
        snapshot["response_usage_confirmed"] = False
    elif proof_state == "truthy_integer":
        snapshot["response_usage_confirmed"] = 1
    elif proof_state == "invalid_attempts":
        snapshot["attempts"] = True
    row.update(success=success, price_snapshot_json=json.dumps(snapshot))
    assert row_cost(row) == ("unknown", Decimal(0))


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
