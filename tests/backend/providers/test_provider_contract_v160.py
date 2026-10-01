"""V16-P01: bounded structured output and owned stream-task lifecycle."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.providers.base import LLMProvider, ProviderCapabilities
from app.providers.provider import ProviderError


class ContractProvider(LLMProvider):
    id = name = model = "contract-test"

    def __init__(self, content: Any = '{"ok": true}', *, blocking: bool = False, failure: bool = False):
        self.content = content
        self.blocking = blocking
        self.failure = failure
        self.calls = 0
        self.chat_task: asyncio.Task[Any] | None = None
        self.stopped = False

    async def chat(self, messages, *, event_callback=None, **kwargs):
        self.calls += 1
        self.chat_task = asyncio.current_task()
        try:
            if event_callback is not None:
                await event_callback("model.delta", {"delta": "test"})
            if self.blocking:
                await asyncio.Event().wait()
            if self.failure:
                raise ProviderError("test failure", "network_error")
            return {"content": self.content}
        finally:
            self.stopped = True

    def get_capabilities(self):
        return ProviderCapabilities(streaming=True, structured_output=True)

    async def health_check(self, api_key=None):
        return {}

    def profile(self):
        return {}


@pytest.mark.parametrize("method", ["stream_chat", "stream"])
def test_v160_stream_close_reclaims_owned_chat_task(method):
    async def exercise():
        provider = ContractProvider(blocking=True)
        stream = getattr(provider, method)([])
        try:
            assert (await anext(stream))["event"] == "model.delta"
            await stream.aclose()
            assert provider.chat_task is not None and provider.chat_task.done()
            assert provider.stopped
        finally:
            if provider.chat_task is not None and not provider.chat_task.done():
                provider.chat_task.cancel()
                await asyncio.gather(provider.chat_task, return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize("method", ["stream_chat", "stream"])
def test_v160_stream_consumer_cancel_reclaims_owned_chat_task(method):
    async def exercise():
        provider = ContractProvider(blocking=True)
        stream = getattr(provider, method)([])
        await anext(stream)
        consumer = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        consumer.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await consumer
            assert provider.chat_task is not None and provider.chat_task.done()
            assert provider.stopped
        finally:
            if provider.chat_task is not None and not provider.chat_task.done():
                provider.chat_task.cancel()
                await asyncio.gather(provider.chat_task, return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", [False, True])
def test_v160_stream_propagates_completion_or_provider_error(failure):
    async def exercise():
        provider = ContractProvider(failure=failure)
        if failure:
            with pytest.raises(ProviderError) as caught:
                _ = [event async for event in provider.stream([])]
            assert caught.value.error_type == "network_error"
        else:
            events = [event async for event in provider.stream([])]
            assert [event["event"] for event in events] == ["model.delta", "model.completed"]
        assert provider.chat_task is not None and provider.chat_task.done()
        assert provider.stopped
    asyncio.run(exercise())


@pytest.mark.parametrize("schema,content", [
    ({"type": "object", "required": ["count"]}, {}),
    ({"type": "object", "properties": {"count": {"type": "integer"}}}, {"count": True}),
    ({"type": "object", "properties": {"count": {"type": "number"}}}, {"count": False}),
    ({"type": "object", "additionalProperties": False}, {"extra": "secret"}),
    ({"properties": {"items": {"type": "array", "items": {"type": "integer"}}}}, {"items": [1, "2"]}),
    ({"properties": {"items": {"minItems": 2, "maxItems": 3}}}, {"items": [1]}),
    ({"properties": {"items": {"maxItems": 1}}}, {"items": [1, 2]}),
    ({"properties": {"text": {"minLength": 2}}}, {"text": "a"}),
    ({"properties": {"text": {"maxLength": 1}}}, {"text": "ab"}),
    ({"properties": {"count": {"minimum": 2}}}, {"count": 1}),
    ({"properties": {"count": {"maximum": 2}}}, {"count": 3}),
    ({"properties": {"count": {"exclusiveMinimum": 2}}}, {"count": 2}),
    ({"properties": {"count": {"exclusiveMaximum": 2}}}, {"count": 2}),
    ({"properties": {"count": {"enum": [1]}}}, {"count": True}),
    ({"properties": {"count": {"const": {"nested": [1]}}}}, {"count": {"nested": [True]}}),
    ({"additionalProperties": {"type": "integer"}}, {"arbitrary": "bad"}),
])
def test_v160_schema_violation_is_rejected(schema, content):
    provider = ContractProvider(json.dumps(content))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider.structured_output([], schema=schema))
    assert caught.value.error_type == "schema_validation_failed"
    assert provider.calls == 1


def test_v160_nested_schema_accepts_nullable_numeric_and_additional_properties():
    schema = {
        "type": "object", "required": ["nested"], "additionalProperties": False,
        "properties": {"nested": {
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": {"type": "object", "properties": {
                "value": {"type": ["integer", "null"], "enum": [1, None]},
                "label": {"type": "string", "minLength": 1, "maxLength": 3},
            }, "required": ["value"], "additionalProperties": {"type": "boolean"}},
        }},
    }
    content = {"nested": [{"value": 1.0, "label": "中文", "flag": False}, {"value": None}]}
    assert asyncio.run(ContractProvider(json.dumps(content)).structured_output([], schema=schema)) == content


@pytest.mark.parametrize("schema,code", [
    ({"$ref": "https://example.invalid/schema"}, "unsupported_schema"),
    ({"properties": {"value": {"pattern": ".*"}}}, "unsupported_schema"),
    ({"unknown_secret_key": "secret"}, "unsupported_schema"),
    ({"type": "made-up"}, "invalid_schema"),
    ({"required": "name"}, "invalid_schema"),
    ({"required": ["name", "name"]}, "invalid_schema"),
    ({"properties": []}, "invalid_schema"),
    ({"items": []}, "invalid_schema"),
    ({"minLength": True}, "invalid_schema"),
    ({"minimum": True}, "invalid_schema"),
    ({"maximum": float("inf")}, "invalid_schema"),
    ({"enum": []}, "invalid_schema"),
])
def test_v160_invalid_or_unsupported_schema_rejected_before_model_call(schema, code):
    provider = ContractProvider()
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider.structured_output([], schema=schema))
    assert caught.value.error_type == code
    assert provider.calls == 0
    assert "secret" not in json.dumps(caught.value.as_dict())


@pytest.mark.parametrize("content", ['{"value":NaN}', '{"value":Infinity}', '{"value":1,"value":2}', '{"secret":"broken'])
def test_v160_strict_json_rejects_nonfinite_duplicate_keys_and_sanitizes_errors(content):
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ContractProvider(content).structured_output([]))
    assert caught.value.error_type == "invalid_json"
    assert "secret" not in json.dumps(caught.value.as_dict())


def test_v160_violation_never_leaks_values_or_property_paths():
    secret = "sk-test_DO_NOT_USE_000000000000"
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ContractProvider(json.dumps({secret: secret})).structured_output(
            [], schema={"properties": {secret: {"type": "integer"}}},
        ))
    assert secret not in json.dumps(caught.value.as_dict())


def test_v160_schema_and_response_depth_are_bounded():
    schema: dict[str, Any] = {"type": "object"}
    for _ in range(80):
        schema = {"properties": {"nested": schema}}
    provider = ContractProvider()
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider.structured_output([], schema=schema))
    assert caught.value.error_type == "structured_output_limit"
    assert provider.calls == 0
    content = '{"nested":' * 80 + "{}" + "}" * 80
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ContractProvider(content).structured_output([]))
    assert caught.value.error_type == "structured_output_limit"


def test_v160_response_size_is_bounded_without_retry():
    provider = ContractProvider(json.dumps({"value": "x" * 1_048_577}))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider.structured_output([]))
    assert caught.value.error_type == "structured_output_limit"
    assert provider.calls == 1


def test_v160_empty_schema_still_validates_json_object_without_mutating_inputs():
    messages = [{"role": "user", "content": "hello"}]
    schema: dict[str, Any] = {}
    assert asyncio.run(ContractProvider().structured_output(messages, schema=schema)) == {"ok": True}
    assert messages == [{"role": "user", "content": "hello"}]
    assert schema == {}


def test_v160_schema_is_snapshotted_before_awaiting_provider():
    schema = {"properties": {"value": {"type": "integer"}}}

    class MutatingProvider(ContractProvider):
        async def chat(self, messages, **kwargs):
            schema["properties"]["value"]["type"] = "string"
            return {"content": '{"value":"not-an-integer"}'}

    with pytest.raises(ProviderError) as caught:
        asyncio.run(MutatingProvider().structured_output([], schema=schema))
    assert caught.value.error_type == "schema_validation_failed"


@pytest.mark.parametrize("schema,content", [
    ({"properties": {"value": False}}, {"value": 1}),
    ({"properties": {"values": {"items": False}}}, {"values": [1]}),
])
def test_v160_nested_boolean_false_schema_rejects(schema, content):
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ContractProvider(content).structured_output([], schema=schema))
    assert caught.value.error_type == "schema_validation_failed"


def test_v160_annotations_are_not_silently_treated_as_assertions():
    schema = {
        "title": "Example", "description": "Description", "$comment": "Metadata",
        "default": {"not": "an assertion"}, "examples": [{"not": "an assertion"}],
        "properties": {"value": True},
    }
    assert asyncio.run(ContractProvider({"value": 3}).structured_output([], schema=schema)) == {"value": 3}


@pytest.mark.parametrize("content", [{"value": float("inf")}, {"value": (1, 2)}, {1: "bad key"}])
def test_v160_preparsed_response_must_still_be_json(content):
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ContractProvider(content).structured_output([]))
    assert caught.value.error_type == "invalid_json"


@pytest.mark.parametrize("case", ["schema_nodes", "output_nodes", "validation_steps", "schema_bytes"])
def test_v160_all_validation_resource_budgets_are_enforced(case, monkeypatch):
    from app.providers import schema_validation

    schema: dict[str, Any] = {"type": "object"}
    content = {"values": [1, 2, 3, 4]}
    before_call = case in {"schema_nodes", "schema_bytes"}
    if case == "schema_nodes":
        monkeypatch.setattr(schema_validation, "MAX_SCHEMA_NODES", 1)
    elif case == "output_nodes":
        monkeypatch.setattr(schema_validation, "MAX_OUTPUT_NODES", 2)
    elif case == "validation_steps":
        monkeypatch.setattr(schema_validation, "MAX_VALIDATION_STEPS", 1)
    else:
        schema["description"] = "x" * 131_073
    provider = ContractProvider(content)
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider.structured_output([], schema=schema))
    assert caught.value.error_type == "structured_output_limit"
    assert provider.calls == (0 if before_call else 1)


@pytest.mark.parametrize("method", ["stream_chat", "stream"])
def test_v160_stream_close_waits_for_async_cleanup_and_retrieves_cleanup_failure(method):
    class CleaningProvider(ContractProvider):
        async def chat(self, messages, *, event_callback=None, **kwargs):
            self.chat_task = asyncio.current_task()
            try:
                await event_callback("model.delta", {"delta": "test"})
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                self.stopped = True
                raise RuntimeError("cleanup failure")

    async def exercise():
        provider = CleaningProvider()
        stream = getattr(provider, method)([])
        await anext(stream)
        await stream.aclose()
        assert provider.chat_task is not None and provider.chat_task.done()
        assert provider.stopped
    asyncio.run(exercise())


def test_v160_stream_backpressure_is_bounded_and_can_be_cancelled():
    class FastProvider(ContractProvider):
        count = 0

        async def chat(self, messages, *, event_callback=None, **kwargs):
            self.chat_task = asyncio.current_task()
            try:
                for _ in range(10_000):
                    await event_callback("model.delta", {"delta": "x"})
                    self.count += 1
            finally:
                self.stopped = True
            return {"content": "done"}

    async def exercise():
        provider = FastProvider()
        stream = provider.stream_chat([])
        await anext(stream)
        await asyncio.sleep(0)
        assert provider.count <= 65
        await stream.aclose()
        assert provider.chat_task is not None and provider.chat_task.done()
        assert provider.stopped
    asyncio.run(exercise())
