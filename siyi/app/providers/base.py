from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, AsyncGenerator, Callable, Literal

from app.providers.schema_validation import StructuredOutputError, parse_output, prepare_schema, validate_output


class FailureCategory(StrEnum):
    MODEL_FAILURE = "MODEL_FAILURE"
    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"
    TOOL_FAILURE = "TOOL_FAILURE"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    PRODUCT_DEFECT = "PRODUCT_DEFECT"
    SECURITY_BLOCK = "SECURITY_BLOCK"
    USER_CANCELLED = "USER_CANCELLED"
    CONFLICT = "CONFLICT"
    MIGRATION_FAILURE = "MIGRATION_FAILURE"


@dataclass(frozen=True)
class ProviderCapabilities:
    chat: bool = True
    streaming: bool | None = None
    native_tool_calls: bool | None = None
    structured_output: bool | None = None
    cancellation: bool = True
    local: bool = False
    vision: bool | None = None
    reasoning: bool | None = None
    json_mode: bool | None = None
    embeddings: bool | None = None
    context_window: int | None = None
    default_max_output_tokens: int | None = None
    source: str = "declared"


CredentialPolicy = Literal["required", "optional", "forbidden"]


@dataclass(frozen=True)
class ProviderRetryPolicy:
    max_retries: int
    backoff: str = "exponential"
    retryable_errors: tuple[str, ...] = (
        "timeout",
        "network_error",
        "rate_limited",
        "server_error",
    )


@dataclass(frozen=True)
class ProviderDescriptor:
    """Canonical, credential-free identity shared by routing, UI and runtime.

    A descriptor deliberately contains only a credential *policy*.  Secret
    values remain request-scoped or in the desktop credential store and can
    never be serialized with provider configuration.
    """

    provider_id: str
    display_name: str
    provider_type: str
    endpoint: str
    model: str
    credential_policy: CredentialPolicy
    capabilities: ProviderCapabilities
    timeout: int
    retry_policy: ProviderRetryPolicy
    local: bool
    health_strategy: str


@dataclass(frozen=True)
class ProviderHealth:
    status: str
    provider: str
    model: str
    latency_ms: int | None = None
    error: str | None = None
    failure_category: FailureCategory | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.failure_category is not None:
            payload["failure_category"] = self.failure_category.value
        return payload


class LLMProvider(ABC):
    id: str
    name: str
    model: str

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        event_callback: Callable[[str, dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.chat(messages, tools=tools, **kwargs)

    def supports_tools(self) -> bool:
        return self.get_capabilities().native_tool_calls is True

    def supports_structured_output(self) -> bool:
        capabilities = self.get_capabilities()
        return capabilities.structured_output is True or capabilities.json_mode is True

    @staticmethod
    def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
        raw = usage or {}
        input_tokens = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
        output_tokens = int(raw.get("output_tokens") or raw.get("completion_tokens") or 0)
        total_tokens = int(raw.get("total_tokens") or input_tokens + output_tokens)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_input_tokens": int(raw.get("cached_input_tokens") or 0),
            "cache_write_tokens": int(raw.get("cache_write_tokens") or 0),
        }

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        self._require_capability("streaming", self.get_capabilities().streaming)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=64)

        async def emit(event: str, data: dict[str, Any]) -> None:
            await queue.put({"event": event, **data})

        task = asyncio.create_task(self.chat(messages, tools=tools, event_callback=emit, **kwargs))
        try:
            while not task.done() or not queue.empty():
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.05)
                except TimeoutError:
                    continue
                if item is not None:
                    yield item
            yield {"event": "model.completed", "message": await task}
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # The body propagates normal provider failures. On early
                # close/cancellation, retrieve cleanup failures without
                # replacing the consumer's original exception.
                pass

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        stream = self.stream_chat(messages, tools=tools, **kwargs)
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()

    async def tool_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._require_capability("tool_call", self.get_capabilities().native_tool_calls)
        return await self.chat(messages, tools=tools, **kwargs)

    async def structured_output(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Validate a bounded JSON Schema subset locally, without auto-retries.

        Unsupported assertions are rejected before generation; supported
        keywords and fixed limits are documented in schema_validation.
        """
        capabilities = self.get_capabilities()
        self._require_capability(
            "structured_output",
            capabilities.structured_output if capabilities.structured_output is not None else capabilities.json_mode,
        )
        try:
            schema_snapshot = prepare_schema(schema)
        except StructuredOutputError as exc:
            from app.providers.provider import ProviderError

            raise ProviderError(str(exc), exc.code) from None
        prepared = list(messages)
        if schema_snapshot is not None:
            prepared = [
                {
                    "role": "system",
                    "content": "Return one JSON object matching this schema: "
                    + json.dumps(schema_snapshot, ensure_ascii=False, separators=(",", ":")),
                },
                *prepared,
            ]
        response = await self.chat(
            prepared,
            response_format={"type": "json_object"},
            **kwargs,
        )
        try:
            value = parse_output(response.get("content"))
            validate_output(value, schema_snapshot)
        except StructuredOutputError as exc:
            from app.providers.provider import ProviderError

            raise ProviderError(str(exc), exc.code) from None
        return value

    async def vision(
        self,
        messages: list[dict[str, Any]],
        *,
        images: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._require_capability("vision", self.get_capabilities().vision)
        prepared = list(messages)
        if images:
            prepared.append({"role": "user", "content": images})
        return await self.chat(prepared, **kwargs)

    async def embedding(
        self,
        inputs: str | list[str],
        **kwargs: Any,
    ) -> list[list[float]]:
        del inputs, kwargs
        self._require_capability("embedding", self.get_capabilities().embeddings)
        raise self._unsupported_capability("embedding")

    async def list_models(self) -> list[dict[str, Any]]:
        profile = self.profile()
        return [{"name": str(model)} for model in profile.get("models") or [] if str(model)]

    def capabilities(self) -> dict[str, Any]:
        declared = asdict(self.get_capabilities())
        return {
            **declared,
            "supports_stream": declared["streaming"],
            "supports_tools": declared["native_tool_calls"],
            "supports_vision": declared["vision"],
            "supports_reasoning": declared["reasoning"],
            "supports_json_mode": (
                declared["json_mode"]
                if declared["json_mode"] is not None
                else declared["structured_output"]
            ),
            "supports_embeddings": declared["embeddings"],
        }

    def estimate_context(
        self,
        messages: list[dict[str, Any]],
        *,
        reserved_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        capabilities = self.get_capabilities()
        estimated_input = self.count_tokens(messages)
        context_window = capabilities.context_window
        default_output = capabilities.default_max_output_tokens
        reserved_output = reserved_output_tokens if reserved_output_tokens is not None else default_output
        remaining = None
        fits = None
        if context_window is not None and reserved_output is not None:
            remaining = context_window - estimated_input - max(0, reserved_output)
            fits = remaining >= 0
        return {
            "provider": self.id,
            "model": self.model,
            "estimated_input_tokens": estimated_input,
            "context_window": context_window,
            "reserved_output_tokens": reserved_output,
            "remaining_tokens": remaining,
            "fits": fits,
            "estimate_is_exact": False,
            "source": capabilities.source,
        }

    @staticmethod
    def cancel(task: asyncio.Task[Any]) -> None:
        task.cancel()

    @abstractmethod
    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @staticmethod
    def _unsupported_capability(name: str) -> Exception:
        from app.providers.provider import ProviderError

        return ProviderError(
            f"当前模型提供方不支持 {name} 能力，请切换提供方或选择兼容模型。",
            "unsupported_capability",
        )

    @classmethod
    def _require_capability(cls, name: str, supported: bool | None) -> None:
        if supported is not True:
            raise cls._unsupported_capability(name)

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        return max(1, (len(serialized) + 3) // 4)

    @abstractmethod
    def profile(self) -> dict[str, Any]:
        raise NotImplementedError
