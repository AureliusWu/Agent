from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from app.providers.base import FailureCategory, LLMProvider, ProviderCapabilities
from app.providers.provider import ProviderError


MOCK_SCENARIOS = (
    "normal",
    "stream",
    "tool_call",
    "structured_output",
    "model_error",
    "timeout",
    "cancelled",
    "empty_response",
    "invalid_json",
    "missing_tool_arguments",
    "invalid_tool_arguments",
    "unknown_tool",
    "duplicate_tool_call",
    "stream_disconnect",
    "context_overflow",
    "truncated_output",
    "health_error",
)


class MockProvider(LLMProvider):
    id = "mock"
    name = "Deterministic Mock"
    model = "siyi-mock-v1"

    def __init__(self, scenario: str = "normal") -> None:
        if scenario not in MOCK_SCENARIOS:
            raise ValueError(f"unknown mock scenario: {scenario}")
        self.scenario = scenario

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.scenario == "timeout":
            raise ProviderError("mock timeout", "timeout", retryable=True)
        if self.scenario == "cancelled":
            raise asyncio.CancelledError
        if self.scenario == "model_error":
            raise ProviderError("mock model failure", "server_error", retryable=True)
        if self.scenario == "context_overflow":
            raise ProviderError("mock context overflow", "context_overflow")
        if self.scenario == "stream_disconnect":
            if event_callback:
                await event_callback("model.delta", {"delta": "partial", "phase": "analysis"})
            raise ProviderError("mock stream disconnected", "network_error", retryable=True)
        if self.scenario == "empty_response":
            raise ProviderError("mock empty response", "empty_response")
        if self.scenario == "invalid_json":
            return {"role": "assistant", "content": "{invalid"}
        if self.scenario == "truncated_output":
            return {"role": "assistant", "content": "partial", "finish_reason": "length"}
        if self.scenario in {
            "tool_call",
            "missing_tool_arguments",
            "invalid_tool_arguments",
            "unknown_tool",
            "duplicate_tool_call",
        }:
            name = "unknown_tool" if self.scenario == "unknown_tool" else (
                str(((tools or [{}])[0].get("function") or {}).get("name") or "read_file")
            )
            arguments = {
                "missing_tool_arguments": "",
                "invalid_tool_arguments": "{invalid",
            }.get(self.scenario, json.dumps({"path": "README.md"}))
            call = {"id": "mock-call-1", "type": "function", "function": {"name": name, "arguments": arguments}}
            calls = [call, {**call, "id": "mock-call-2"}] if self.scenario == "duplicate_tool_call" else [call]
            return {"role": "assistant", "content": None, "tool_calls": calls, "_metrics": {"provider": self.id}}
        if self.scenario == "structured_output":
            return {"role": "assistant", "content": '{"status":"ok","provider":"mock"}'}
        content = "mock response"
        if event_callback and self.scenario == "stream":
            for delta in ("mock ", "response"):
                await event_callback("model.delta", {"delta": delta, "phase": "analysis"})
        return {"role": "assistant", "content": content, "_metrics": {"provider": self.id}}

    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        if self.scenario == "health_error":
            return {
                "status": "error",
                "provider": self.id,
                "model": self.model,
                "error": "mock health error",
                "failure_category": FailureCategory.ENVIRONMENT_FAILURE.value,
            }
        return {
            "status": "ok",
            "provider": self.id,
            "model": self.model,
            "latency_ms": 0,
            "capabilities": self.capabilities(),
        }

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            native_tool_calls=True,
            structured_output=True,
            local=True,
            vision=False,
            reasoning=False,
            json_mode=True,
            embeddings=False,
            context_window=65_536,
            default_max_output_tokens=8_192,
            source="deterministic_contract",
        )

    def profile(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "api_format": "in-process",
            "request_url": "",
            "chat_endpoint": "",
            "credential_env": "",
            "default_model": self.model,
            "models": [self.model],
            "scenario": self.scenario,
            "scenarios": list(MOCK_SCENARIOS),
            "capabilities": {
                "chat": True,
                "streaming": True,
                "native_tool_calls": True,
                "structured_output": True,
                "cancellation": True,
                "local": True,
            },
            "local": True,
        }
