"""Response completion, not semantic success, confirms failed-call usage.

All HTTP/DNS is synthetic; accounting and reservation assertions use SQLite in
the test-owned directory. These tests never require a real model credential.
"""
import asyncio
import ipaddress
import json
import socket
import uuid
from decimal import Decimal

import httpx
import pytest

from app.config import settings
from app.database import connect, init_db, now_iso, rows
from app.providers.capabilities import provider_identity
from app.providers.costs import cost_summary, pricing_snapshot, row_cost, task_cost_fields
from app.providers.provider import ProviderError, completion
from app.runtime.cost_budget import CostBudgetBlocked, reserve
from app.runtime.task_leases import (
    acquire_task_lease,
    bind_task_lease,
    release_task_lease,
    reset_task_lease,
)


BASE_URL = "https://provider.example/v1"
PROVIDER = "openai_compatible"
MODEL = "synthetic-response-usage-v160"
SYNTHETIC_KEY = "synthetic-test-key-not-a-credential"
PRIVATE_REASONING = "PRIVATE_RESPONSE_USAGE_SENTINEL"
UNTRUSTED_USAGE_SECRET = "UNTRUSTED_USAGE_SECRET_SENTINEL"
USAGE = {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}
EXPECTED_COST = Decimal("0.000034")
_MISSING = object()


def _body(usage=_MISSING, *, finish="length", reasoning=True):
    message = {"role": "assistant", "content": ""}
    if reasoning:
        message["reasoning_content"] = PRIVATE_REASONING
    result = {"choices": [{"message": message, "finish_reason": finish}]}
    if usage is not _MISSING:
        result["usage"] = usage
    return result


def _event(value):
    return "data: " + json.dumps(value)


class _Stream:
    status_code = 200
    headers = {}

    def __init__(self, usage=_MISSING, *, done=True, tail_error=None,
                 exit_error=None, finish="length", reasoning=True):
        self.lines = []
        if reasoning:
            self.lines.append(_event({"choices": [{"delta": {"reasoning_content": PRIVATE_REASONING}}]}))
        self.lines.append(_event({"choices": [{"delta": {}, "finish_reason": finish}]}))
        if usage is not _MISSING:
            self.lines.append(_event({"choices": [], "usage": usage}))
        if done:
            self.lines.append("data: [DONE]")
        self.tail_error = tail_error
        self.exit_error = exit_error
        self.entered = self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *_args):
        self.exited += 1
        if self.exit_error is not None:
            raise self.exit_error
        return False

    async def aiter_lines(self):
        for line in self.lines:
            yield line
        if self.tail_error is not None:
            raise self.tail_error


class _Transport:
    def __init__(self):
        self.queue = []
        self.calls = []

    def take(self, method, url, kwargs):
        assert method == "POST"
        assert url == BASE_URL + "/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer " + SYNTHETIC_KEY
        self.calls.append(kwargs["json"])
        assert self.queue, "Unexpected extra HTTP request"
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def isolated_usage_transport(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_path", tmp_path / "response-usage.db")
    scoped_key = f"{PROVIDER}:{provider_identity(BASE_URL)[1]}:{MODEL}"
    monkeypatch.setattr(settings, "model_pricing_json", json.dumps({scoped_key: {"input": 2, "output": 4}}))
    monkeypatch.setenv("SIYI_ALLOW_PAID_API", "false")

    async def resolve(_host, _port):
        return (ipaddress.ip_address("8.8.8.8"),)

    def forbidden_dns(*_args, **_kwargs):
        raise AssertionError("Real DNS is forbidden in response-usage tests")

    async def no_retry_delay(_seconds):
        return None

    transport = _Transport()

    class Client:
        def __init__(self, **kwargs):
            assert kwargs.get("follow_redirects") is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            return transport.take("POST", url, kwargs)

        def stream(self, method, url, **kwargs):
            return transport.take(method, url, kwargs)

    monkeypatch.setattr("app.security.network_security._resolve_host", resolve)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    monkeypatch.setattr("app.providers.provider.httpx.AsyncClient", Client)
    monkeypatch.setattr("app.providers.provider.asyncio.sleep", no_retry_delay)
    init_db()
    yield transport
    assert not transport.queue, "Synthetic HTTP response was not consumed"


def _call(*, streaming=False, max_retries=0, **kwargs):
    return asyncio.run(completion(
        [{"role": "user", "content": "synthetic usage accounting check"}],
        SYNTHETIC_KEY, base_url=BASE_URL, model=MODEL,
        provider_id_override=PROVIDER, max_tokens=128, max_retries=max_retries,
        event_callback=(lambda *_args: None) if streaming else None, **kwargs,
    ))


def _stored():
    records = rows("SELECT * FROM model_runs ORDER BY id")
    assert len(records) == 1
    return records[0], json.loads(records[0]["price_snapshot_json"])


def _assert_cost(status, amount, *, confirmed=None, error_type=None):
    record, snapshot = _stored()
    assert record["success"] == 0
    if error_type is not None:
        assert record["error_type"] == error_type
    assert snapshot["cost_status"] == status
    assert Decimal(snapshot["known_cost_usd_decimal"]) == amount
    assert row_cost(record) == (status, amount)
    if confirmed is not None:
        assert snapshot["response_usage_confirmed"] is confirmed
    summary = cost_summary([record])
    assert summary["known_cost_usd"] == float(amount)
    assert summary["unknown_cost_requests"] == int(status != "known")
    assert summary["estimated_cost_usd"] == (float(amount) if status == "known" else None)
    assert PRIVATE_REASONING not in record["price_snapshot_json"]
    assert SYNTHETIC_KEY not in record["price_snapshot_json"]
    return record, snapshot


@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse-done"])
def test_complete_response_semantic_failure_preserves_error_and_known_cost(isolated_usage_transport, streaming):
    response = _Stream(dict(USAGE)) if streaming else httpx.Response(200, json=_body(dict(USAGE)))
    isolated_usage_transport.queue.append(response)
    with pytest.raises(ProviderError) as caught:
        _call(streaming=streaming)
    assert caught.value.error_type == "empty_after_reasoning"
    assert caught.value.retryable is False
    assert caught.value.details == {"finish_reason": "length", "usage": USAGE}
    assert PRIVATE_REASONING not in str(caught.value.as_dict())
    record, snapshot = _assert_cost("known", EXPECTED_COST, confirmed=True, error_type="empty_after_reasoning")
    assert snapshot["reason"] == "complete_response_usage"
    assert (record["input_tokens"], record["output_tokens"], record["total_tokens"]) == (3, 7, 10)
    assert len(isolated_usage_transport.calls) == 1
    if streaming:
        assert response.entered == response.exited == 1


@pytest.mark.parametrize("usage", [
    _MISSING, {}, {"prompt_tokens": 3},
    {**USAGE, "total_tokens": 11}, {**USAGE, "total_tokens": True},
    {**USAGE, "total_tokens": 10.0}, {**USAGE, "completion_tokens": -7},
], ids=["missing", "empty", "incomplete", "wrong-total", "boolean-total", "float-total", "negative-output"])
@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse-done"])
def test_complete_semantic_failure_with_incomplete_usage_stays_unknown(isolated_usage_transport, usage, streaming):
    isolated_usage_transport.queue.append(_Stream(usage) if streaming else httpx.Response(200, json=_body(usage)))
    with pytest.raises(ProviderError) as caught:
        _call(streaming=streaming)
    assert caught.value.error_type == "empty_after_reasoning"
    _assert_cost("unknown", Decimal(0), error_type="empty_after_reasoning")


@pytest.mark.parametrize("details", [
    UNTRUSTED_USAGE_SECRET, [UNTRUSTED_USAGE_SECRET], True,
    {"cached_tokens": 2, "secret": UNTRUSTED_USAGE_SECRET,
     "reasoning_content": UNTRUSTED_USAGE_SECRET,
     "nested": {"api_key": UNTRUSTED_USAGE_SECRET}},
], ids=["string", "list", "boolean", "dict-with-untrusted-fields"])
@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse-done"])
def test_usage_projection_drops_untrusted_fields_and_tolerates_malformed_details(
    isolated_usage_transport, details, streaming,
):
    usage = {**USAGE, "api_key": UNTRUSTED_USAGE_SECRET,
             "reasoning": UNTRUSTED_USAGE_SECRET,
             "reasoning_content": UNTRUSTED_USAGE_SECRET,
             "arbitrary": {"secret": UNTRUSTED_USAGE_SECRET},
             "prompt_tokens_details": details}
    isolated_usage_transport.queue.append(_Stream(usage) if streaming else httpx.Response(200, json=_body(usage)))
    with pytest.raises(ProviderError) as caught:
        _call(streaming=streaming)
    assert caught.value.error_type == "empty_after_reasoning"
    assert caught.value.retryable is False
    record, snapshot = _assert_cost("known", EXPECTED_COST, confirmed=True, error_type="empty_after_reasoning")
    metrics = caught.value._metrics
    assert metrics["cost_status"] == "known"
    assert metrics["known_cost_usd"] == float(EXPECTED_COST)
    expected_usage = dict(USAGE)
    if isinstance(details, dict):
        expected_usage["prompt_tokens_details"] = {"cached_tokens": 2}
    assert metrics["usage"] == expected_usage
    assert record["cached_input_tokens"] == (2 if isinstance(details, dict) else 0)
    encoded = json.dumps({"metrics": metrics, "record": record, "snapshot": snapshot,
                          "error": caught.value.as_dict()}, sort_keys=True)
    assert UNTRUSTED_USAGE_SECRET not in encoded
    assert PRIVATE_REASONING not in encoded
    assert SYNTHETIC_KEY not in encoded


@pytest.mark.parametrize("cache_usage,cached,written", [
    ({"cache_read_input_tokens": 2}, 2, 0),
    ({"cache_creation_input_tokens": 1}, 0, 1),
    ({"prompt_cache_write_tokens": 1}, 0, 1),
    ({"prompt_tokens_details": {"cached_tokens": 2}}, 2, 0),
    ({"prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 1}, 2, 0),
], ids=["cache-read", "cache-create", "prompt-cache-write", "nested-cached", "prompt-cache-hit-miss"])
@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse-done"])
def test_usage_projection_preserves_valid_cache_counters(
    isolated_usage_transport, cache_usage, cached, written, streaming,
):
    usage = {**USAGE, **cache_usage}
    isolated_usage_transport.queue.append(_Stream(usage) if streaming else httpx.Response(200, json=_body(usage)))
    with pytest.raises(ProviderError) as caught:
        _call(streaming=streaming)
    assert caught.value.error_type == "empty_after_reasoning"
    record, _snapshot = _assert_cost("known", EXPECTED_COST, confirmed=True, error_type="empty_after_reasoning")
    assert caught.value._metrics["usage"] == usage
    assert record["cached_input_tokens"] == cached
    assert record["uncached_input_tokens"] == USAGE["prompt_tokens"] - cached
    assert record["cache_write_tokens"] == written


@pytest.mark.parametrize("invalid_total", [None, "10", True, 10.0, -1, 11],
                         ids=["null", "string", "boolean", "float", "negative", "mismatched-integer"])
@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse-done"])
def test_usage_projection_cannot_turn_invalid_original_total_into_known_cost(
    isolated_usage_transport, invalid_total, streaming,
):
    usage = {**USAGE, "total_tokens": invalid_total,
             "secret": UNTRUSTED_USAGE_SECRET,
             "prompt_tokens_details": {"cached_tokens": 2, "secret": UNTRUSTED_USAGE_SECRET}}
    isolated_usage_transport.queue.append(_Stream(usage) if streaming else httpx.Response(200, json=_body(usage)))
    with pytest.raises(ProviderError) as caught:
        _call(streaming=streaming)
    assert caught.value.error_type == "empty_after_reasoning"
    record, snapshot = _assert_cost("unknown", Decimal(0), confirmed=True, error_type="empty_after_reasoning")
    assert snapshot["usage_complete"] is False
    metrics = caught.value._metrics
    assert metrics["cost_status"] == "unknown"
    assert metrics["estimated_cost_usd"] is None
    assert "total_tokens" in metrics["usage"]
    assert metrics["usage"]["total_tokens"] == (invalid_total if type(invalid_total) is int and invalid_total >= 0 else None)
    assert UNTRUSTED_USAGE_SECRET not in json.dumps({"metrics": metrics, "record": record, "snapshot": snapshot})


@pytest.mark.parametrize("status_code", [302, 307])
def test_json_redirect_is_not_followed_or_confirmed_even_same_origin(isolated_usage_transport, status_code):
    body = _body(dict(USAGE), finish="stop", reasoning=False)
    body["choices"][0]["message"]["content"] = "synthetic redirect body"
    isolated_usage_transport.queue.append(httpx.Response(
        status_code, headers={"Location": BASE_URL + "/redirected/chat/completions"}, json=body,
    ))
    with pytest.raises(ProviderError):
        _call()
    _assert_cost("unknown", Decimal(0), confirmed=False)
    assert len(isolated_usage_transport.calls) == 1


@pytest.mark.parametrize("status_code", [300, 304])
def test_json_non_2xx_with_complete_body_and_usage_is_not_confirmed(isolated_usage_transport, status_code):
    body = _body(dict(USAGE), finish="stop", reasoning=False)
    body["choices"][0]["message"]["content"] = "synthetic non-2xx body"
    isolated_usage_transport.queue.append(httpx.Response(status_code, json=body))
    with pytest.raises(ProviderError):
        _call()
    _assert_cost("unknown", Decimal(0), confirmed=False)
    assert len(isolated_usage_transport.calls) == 1


@pytest.mark.parametrize("status_code", [300, 302, 304, 307])
def test_stream_non_2xx_with_done_and_usage_is_not_confirmed(isolated_usage_transport, status_code):
    stream = _Stream(dict(USAGE), finish="stop", reasoning=False)
    stream.status_code = status_code
    stream.headers = {"location": BASE_URL + "/redirected/chat/completions"}
    stream.lines.insert(0, _event({"choices": [{"delta": {"content": "synthetic non-2xx body"}}]}))
    isolated_usage_transport.queue.append(stream)
    with pytest.raises(ProviderError):
        _call(streaming=True)
    _assert_cost("unknown", Decimal(0), confirmed=False)
    assert len(isolated_usage_transport.calls) == 1
    assert stream.entered == stream.exited == 1


def test_stream_usage_without_done_is_not_confirmed(isolated_usage_transport):
    isolated_usage_transport.queue.append(_Stream(dict(USAGE), done=False))
    with pytest.raises(ProviderError):
        _call(streaming=True)
    _assert_cost("unknown", Decimal(0), confirmed=False)


def test_stream_timeout_after_usage_does_not_confirm_cost(isolated_usage_transport):
    isolated_usage_transport.queue.append(_Stream(dict(USAGE), done=False, tail_error=httpx.ReadTimeout("synthetic timeout")))
    with pytest.raises(ProviderError) as caught:
        _call(streaming=True)
    assert caught.value.error_type == "timeout"
    _assert_cost("unknown", Decimal(0), confirmed=False, error_type="timeout")


def test_stream_cancellation_after_usage_does_not_confirm_cost(isolated_usage_transport):
    isolated_usage_transport.queue.append(_Stream(dict(USAGE), done=False, tail_error=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        _call(streaming=True)
    _assert_cost("unknown", Decimal(0), confirmed=False, error_type="cancelled")


@pytest.mark.parametrize("exit_error", [httpx.ReadTimeout("synthetic exit timeout"), RuntimeError("synthetic exit failure")],
                         ids=["timeout", "unexpected"])
def test_stream_done_but_response_context_exit_failure_stays_unknown(isolated_usage_transport, exit_error):
    stream = _Stream(dict(USAGE), exit_error=exit_error)
    isolated_usage_transport.queue.append(stream)
    with pytest.raises((ProviderError, RuntimeError)):
        _call(streaming=True)
    _assert_cost("unknown", Decimal(0), confirmed=False)
    assert stream.entered == stream.exited == 1


@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse"])
def test_retry_without_usage_never_reuses_previous_attempt_usage(isolated_usage_transport, streaming):
    if streaming:
        isolated_usage_transport.queue.extend([
            _Stream(dict(USAGE), finish="stop", reasoning=False), _Stream(),
        ])
    else:
        isolated_usage_transport.queue.extend([
            httpx.Response(200, json=_body(dict(USAGE), finish="stop", reasoning=False)),
            httpx.Response(200, json=_body()),
        ])
    with pytest.raises(ProviderError) as caught:
        _call(streaming=streaming, max_retries=1)
    assert caught.value.error_type == "empty_after_reasoning"
    record, snapshot = _assert_cost("unknown", Decimal(0))
    assert record["retry_count"] == 1
    assert snapshot["attempts"] == 2
    assert record["input_tokens"] == record["output_tokens"] == record["total_tokens"] == 0
    assert len(isolated_usage_transport.calls) == 2


@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse"])
def test_multiple_attempts_final_complete_failed_usage_is_only_partial(isolated_usage_transport, streaming):
    first_usage = {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}
    if streaming:
        isolated_usage_transport.queue.extend([
            _Stream(first_usage, finish="stop", reasoning=False), _Stream(dict(USAGE)),
        ])
    else:
        isolated_usage_transport.queue.extend([
            httpx.Response(200, json=_body(first_usage, finish="stop", reasoning=False)),
            httpx.Response(200, json=_body(dict(USAGE))),
        ])
    with pytest.raises(ProviderError) as caught:
        _call(streaming=streaming, max_retries=1)
    assert caught.value.error_type == "empty_after_reasoning"
    record, snapshot = _assert_cost("partial", EXPECTED_COST, confirmed=True)
    assert snapshot["reason"] == "retry_response_usage_unknown"
    assert snapshot["attempts"] == 2
    assert record["retry_count"] == 1
    assert (record["input_tokens"], record["output_tokens"]) == (3, 7)
    assert len(isolated_usage_transport.calls) == 2


@pytest.fixture
def leased_usage_task():
    stamp = now_iso()
    task_id = uuid.uuid4().hex
    with connect() as db:
        cursor = db.execute("INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                            ("synthetic usage", "", "ask", stamp, stamp))
        conversation_id = cursor.lastrowid
        db.execute("INSERT INTO agent_tasks(id,conversation_id,status,prompt,cost_budget_limit,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (task_id, conversation_id, "running", "synthetic", 1.0, stamp, stamp))
    lease = acquire_task_lease(task_id, ttl_seconds=60)
    token = bind_task_lease(lease)
    try:
        yield task_id, conversation_id
    finally:
        reset_task_lease(token)
        release_task_lease(lease)


def test_bounded_complete_semantic_failure_settles_reservation_without_retry(isolated_usage_transport, leased_usage_task):
    task_id, conversation_id = leased_usage_task
    isolated_usage_transport.queue.append(httpx.Response(200, json=_body(dict(USAGE))))
    with pytest.raises(ProviderError) as caught:
        _call(task_id=task_id, conversation_id=conversation_id, max_retries=5)
    assert caught.value.error_type == "empty_after_reasoning"
    _assert_cost("known", EXPECTED_COST, confirmed=True)
    task = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0]
    assert task_cost_fields(task)["pending_cost_requests"] == 0
    assert len(isolated_usage_transport.calls) == 1


def test_bounded_unknown_failed_response_still_blocks_next_reservation(isolated_usage_transport, leased_usage_task):
    task_id, conversation_id = leased_usage_task
    isolated_usage_transport.queue.append(httpx.Response(200, json=_body()))
    with pytest.raises(ProviderError) as caught:
        _call(task_id=task_id, conversation_id=conversation_id)
    assert caught.value.error_type == "empty_after_reasoning"
    _assert_cost("unknown", Decimal(0))
    price = pricing_snapshot(provider=PROVIDER, base_url=BASE_URL, model=MODEL)
    with pytest.raises(CostBudgetBlocked) as blocked:
        reserve(task_id=task_id, price=price, input_tokens=1, output_tokens=1)
    assert blocked.value.code == "cost_usage_unknown"
    assert len(isolated_usage_transport.calls) == 1


def test_complete_failed_response_persistence_failure_retains_uncertain_reservation(
    isolated_usage_transport, leased_usage_task, monkeypatch,
):
    task_id, conversation_id = leased_usage_task
    isolated_usage_transport.queue.append(httpx.Response(200, json=_body(dict(USAGE))))

    def storage_unavailable(**_kwargs):
        raise RuntimeError("synthetic storage unavailable")

    monkeypatch.setattr("app.providers.provider.record_model_run", storage_unavailable)
    with pytest.raises(ProviderError) as caught:
        _call(task_id=task_id, conversation_id=conversation_id)
    assert caught.value.error_type == "cost_usage_unknown"
    assert not rows("SELECT * FROM model_runs")
    task = rows("SELECT * FROM agent_tasks WHERE id=?", (task_id,))[0]
    assert task_cost_fields(task)["pending_cost_requests"] == 1
    price = pricing_snapshot(provider=PROVIDER, base_url=BASE_URL, model=MODEL)
    with pytest.raises(CostBudgetBlocked) as blocked:
        reserve(task_id=task_id, price=price, input_tokens=1, output_tokens=1)
    assert blocked.value.code == "cost_usage_unknown"
    assert len(isolated_usage_transport.calls) == 1
