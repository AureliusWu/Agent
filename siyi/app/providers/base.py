from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, AsyncIterator, Callable


class FailureCategory(StrEnum):
    MODEL_FAILURE = "MODEL_FAILURE"
    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"
    TOOL_FAILURE = "TOOL_FAILURE"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"


@dataclass(frozen=True)
class ProviderCapabilities:
    chat: bool = True
    streaming: bool | None = None
    native_tool_calls: bool | None = None
    structured_output: bool | None = None
    cancellation: bool = True
    local: bool = False


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

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def emit(event: str, data: dict[str, Any]) -> None:
            await queue.put({"event": event, **data})

        task = asyncio.create_task(self.chat(messages, tools=tools, event_callback=emit, **kwargs))
        while not task.done() or not queue.empty():
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            if item is not None:
                yield item
        yield {"event": "model.completed", "message": await task}

    async def tool_call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.chat(messages, tools=tools, **kwargs)

    async def structured_output(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        prepared = list(messages)
        if schema:
            prepared = [
                {
                    "role": "system",
                    "content": "Return one JSON object matching this schema: "
                    + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                },
                *prepared,
            ]
        response = await self.chat(
            prepared,
            response_format={"type": "json_object"},
            **kwargs,
        )
        content = response.get("content")
        try:
            value = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError) as exc:
            from app.providers.provider import ProviderError

            raise ProviderError("模型未返回有效 JSON", "invalid_json") from exc
        if not isinstance(value, dict):
            from app.providers.provider import ProviderError

            raise ProviderError("结构化输出必须是 JSON 对象", "invalid_response")
        return value

    @staticmethod
    def cancel(task: asyncio.Task[Any]) -> None:
        task.cancel()

    @abstractmethod
    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        return max(1, (len(serialized) + 3) // 4)

    @abstractmethod
    def profile(self) -> dict[str, Any]:
        raise NotImplementedError
