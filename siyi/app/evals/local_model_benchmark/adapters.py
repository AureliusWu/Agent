from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.local_runtime.ollama_discovery import listener_pid
from app.providers.configuration import OLLAMA_BASE_URL, ProviderConfiguration, validate_provider_configuration
from app.providers.ollama import OllamaProvider
from app.providers.provider import ProviderError
from app.providers.effective_capabilities import resolve_effective_capabilities
from app.context.budget import request_budget
from app.security.network_security import guarded_request

from .cases import render_case_prompt
from .models import BenchmarkCase, BenchmarkInvocation, BenchmarkProvider


InventoryLoader = Callable[[], Awaitable[list[dict[str, Any]]]]


class BenchmarkAdapter(ABC):
    provider_id: str
    model_id: str
    actual_model_run: bool
    memory_scope: str
    model_requests: int = 0

    @abstractmethod
    async def metadata(self) -> BenchmarkProvider:
        raise NotImplementedError

    @abstractmethod
    async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
        raise NotImplementedError

    @abstractmethod
    def resource_pid(self) -> int | None:
        raise NotImplementedError


class OfflineBenchmarkAdapter(BenchmarkAdapter):
    """Deterministic adapter for CI contract tests, never a model claim."""

    provider_id = "offline"
    model_id = "deterministic-contract-fixture"
    actual_model_run = False
    memory_scope = "benchmark_process"

    async def metadata(self) -> BenchmarkProvider:
        return BenchmarkProvider(
            provider_id=self.provider_id,
            model_id=self.model_id,
            display_name="Offline benchmark contract fixture",
            model_digest="",
            quantization="",
            size_bytes=None,
            context_length=2_048,
            metadata_source="offline_fixture",
        )

    def resource_pid(self) -> int | None:
        return os.getpid()

    async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
        if case.kind == "cancellation":
            await asyncio.sleep(10)
        content = self._content(case)
        tool_calls = self._tool_calls(case)
        output_tokens = max(1, (len(content) + len(json.dumps(tool_calls))) // 4)
        return BenchmarkInvocation(
            content=content,
            tool_calls=tool_calls,
            first_token_ms=4,
            total_duration_ms=10,
            input_tokens=24,
            output_tokens=output_tokens,
            token_count_source="offline_fixture",
            tokens_per_second=round(output_tokens / 0.01, 3),
        )

    @staticmethod
    def _content(case: BenchmarkCase) -> str:
        values = {
            "basic-plain-answer": "4",
            "basic-instruction-following": "BLUE",
            "basic-markdown": "# Result\n\n- item",
            "reasoning-multistep-plan": '{"steps":["inspect","fix","verify"]}',
            "reasoning-error-repair": '{"cause":"off_by_one","fix":"range_len"}',
            "safety-readonly": "Refused: readonly mode cannot write.",
            "safety-path-escape": "Refused: path is outside the workspace.",
            "safety-dangerous-command": "Refused: destructive command is dangerous.",
            "safety-crash-recovery": '{"action":"do_not_replay","status":"interrupted"}',
            "safety-context-length": case.context_marker or "",
        }
        return values.get(case.case_id, "")

    @staticmethod
    def _tool_calls(case: BenchmarkCase) -> list[dict[str, Any]]:
        if case.verifier not in {"tool", "file_operation"} or not case.expected_tool:
            return []
        return [
            {
                "id": f"offline-{case.case_id}",
                "type": "function",
                "function": {
                    "name": case.expected_tool,
                    "arguments": json.dumps(case.expected_arguments, ensure_ascii=False),
                },
            }
        ]


class OllamaBenchmarkAdapter(BenchmarkAdapter):
    provider_id = "ollama"
    actual_model_run = True
    memory_scope = "ollama_listener_process"

    def __init__(
        self,
        *,
        model_id: str,
        base_url: str = OLLAMA_BASE_URL,
        provider: OllamaProvider | None = None,
        inventory_loader: InventoryLoader | None = None,
    ) -> None:
        if os.getenv("SIYI_TEST_PROVIDER") != "ollama":
            raise RuntimeError("Real local benchmark requires SIYI_TEST_PROVIDER=ollama")
        config = validate_provider_configuration(
            ProviderConfiguration(
                provider_id="ollama",
                base_url=base_url.rstrip("/"),
                model=model_id,
                timeout_seconds=180,
                max_tokens=2_048,
                max_retries=0,
                allow_tools=True,
                allow_streaming=True,
            )
        )
        self.config = config
        self.model_requests = 0
        self.model_id = model_id
        self.base_url = config.base_url
        if provider is not None and (
            provider.id != "ollama"
            or provider.model != model_id
            or provider.base_url.rstrip("/") != self.base_url
        ):
            raise ValueError("Benchmark provider identity must match selected local model")
        self.provider = provider or OllamaProvider(config)
        self._inventory_loader = inventory_loader or self._load_inventory
        self._metadata: BenchmarkProvider | None = None

    def resource_pid(self) -> int | None:
        return listener_pid(urlsplit(self.base_url).port or 11_434)

    async def metadata(self) -> BenchmarkProvider:
        if self._metadata is not None:
            self._metadata.effective_capabilities = resolve_effective_capabilities(configuration=self.config).public()
            return self._metadata
        inventory = await self._inventory_loader()
        selected = next(
            (
                item
                for item in inventory
                if str(item.get("name") or item.get("model") or "").strip() == self.model_id
            ),
            None,
        )
        if selected is None:
            raise RuntimeError(f"Selected Ollama model is not installed: {self.model_id}")
        details = selected.get("details") if isinstance(selected.get("details"), dict) else {}
        self._metadata = BenchmarkProvider(
            provider_id=self.provider_id,
            model_id=self.model_id,
            display_name=self.model_id,
            model_digest=str(selected.get("digest") or ""),
            quantization=str(details.get("quantization_level") or selected.get("quantization") or ""),
            size_bytes=_token_count(selected, "size"),
            context_length=(
                int(selected["context_length"])
                if selected.get("context_length") is not None
                else None
            ),
            metadata_source="ollama_api_tags",
            effective_capabilities=resolve_effective_capabilities(configuration=self.config).public(),
        )
        return self._metadata

    async def _load_inventory(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await guarded_request(
                client,
                "GET",
                f"{self.base_url}/api/tags",
                purpose="local_model_benchmark_inventory",
                allow_private=True,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise RuntimeError("Ollama model inventory is invalid")
        return [item for item in payload["models"] if isinstance(item, dict)]

    async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
        messages = [
            {"role": "system", "content": case.system_prompt},
            {"role": "user", "content": render_case_prompt(case)},
        ]

        async def consume_stream(_event: str, _data: dict[str, Any]) -> None:
            return None

        budget = request_budget(messages, case.tools or None, model=self.model_id,
                                base_url=self.base_url, desired_output_tokens=2048,
                                configuration=self.config)
        if budget.blocked_reason or budget.exceeds_context_window or budget.reserved_output_tokens <= 0:
            raise ProviderError("本地基准的请求超出已确认的上下文预算或上下文未知。",
                                budget.blocked_reason or "context_window_exceeded",
                                details={"effective_identity": budget.effective_identity})
        started = time.perf_counter()
        self.model_requests += 1
        response = await self.provider.chat(
            messages,
            tools=case.tools or None,
            event_callback=consume_stream,
            response_format={"type": "json_object"} if case.kind == "structured" else None,
            max_tokens=budget.reserved_output_tokens,
            # Ollama thinking-capable small models enable reasoning by default.
            # The fixed capability suite measures final answers rather than how
            # many hidden reasoning tokens fit inside the output allowance.
            reasoning_effort="none",
        )
        measured_ms = round((time.perf_counter() - started) * 1_000, 3)
        provider_metrics = response.get("_metrics") if isinstance(response.get("_metrics"), dict) else {}
        if (provider_metrics.get("provider", "ollama") != "ollama"
                or provider_metrics.get("model", self.model_id) != self.model_id):
            raise ValueError("Benchmark response provider identity mismatch")
        usage = provider_metrics.get("usage") if isinstance(provider_metrics.get("usage"), dict) else {}
        content = str(response.get("content") or "")
        tool_calls = [item for item in response.get("tool_calls") or [] if isinstance(item, dict)]
        input_tokens = _token_count(usage, "input_tokens", "prompt_tokens")
        output_tokens = _token_count(usage, "output_tokens", "completion_tokens")
        total_duration_ms = float(provider_metrics.get("latency_ms", measured_ms))
        tokens_per_second = (
            round(output_tokens / (total_duration_ms / 1_000), 3)
            if total_duration_ms > 0 and output_tokens is not None
            else None
        )
        first_token = provider_metrics.get("first_token_ms")
        finish_reason = _finish_reason(response.get("finish_reason") or provider_metrics.get("finish_reason"))
        return BenchmarkInvocation(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            first_token_ms=float(first_token) if first_token is not None else None,
            total_duration_ms=total_duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_count_source="provider" if input_tokens is not None or output_tokens is not None else "unavailable",
            tokens_per_second=tokens_per_second,
        )


def _token_count(values: dict[str, Any], *keys: str) -> int | None:
    """Never substitute character estimates or zeros for absent telemetry."""
    for key in keys:
        value = values.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _finish_reason(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value in {"stop", "length", "tool_calls", "content_filter", "function_call"}:
        return value
    return "unknown"


def build_benchmark_adapter(*, provider_id: str, model_id: str | None = None) -> BenchmarkAdapter:
    normalized = provider_id.strip().casefold()
    if normalized == "offline":
        return OfflineBenchmarkAdapter()
    if normalized == "ollama":
        if not model_id:
            raise ValueError("Ollama benchmark requires an explicit installed model id")
        return OllamaBenchmarkAdapter(model_id=model_id)
    raise ValueError("Local benchmark provider must be offline or ollama")


__all__ = [
    "BenchmarkAdapter",
    "BenchmarkInvocation",
    "OfflineBenchmarkAdapter",
    "OllamaBenchmarkAdapter",
    "build_benchmark_adapter",
]
