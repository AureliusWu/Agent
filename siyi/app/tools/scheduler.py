from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.runtime.task_events import emit_task_event
from app.tools.registry import REGISTRY, ConcurrencyPolicy


ToolCall = dict[str, Any]
ToolResult = dict[str, Any]
ToolInvoker = Callable[[ToolCall], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ScheduledResult:
    call_id: str
    result: ToolResult
    index: int


def concurrency_policy(name: str) -> ConcurrencyPolicy:
    spec = REGISTRY.get(name)
    return spec.concurrency_policy if spec else "serial"


class ToolScheduler:
    def __init__(self, task_id: str, *, max_parallel: int = 4) -> None:
        self.task_id = task_id
        self._parallel = asyncio.Semaphore(max(1, max_parallel))

    async def _invoke(self, index: int, call: ToolCall, invoke: ToolInvoker) -> ScheduledResult:
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        call_id = str(call.get("id") or f"call-{index}")
        emit_task_event(self.task_id, "tool.scheduler.started", {"call_id": call_id, "tool": name, "index": index})
        try:
            async with self._parallel:
                result = await invoke(call)
        except asyncio.CancelledError:
            emit_task_event(self.task_id, "tool.scheduler.cancelled", {"call_id": call_id, "tool": name, "index": index})
            raise
        except Exception as exc:
            result = {"success": False, "status": "error", "error_code": "tool_scheduler_error", "error_message": str(exc), "retryable": False}
        emit_task_event(
            self.task_id,
            "tool.scheduler.completed",
            {"call_id": call_id, "tool": name, "index": index, "success": bool(result.get("success"))},
        )
        return ScheduledResult(call_id, result, index)

    async def execute(self, calls: list[ToolCall], invoke: ToolInvoker) -> list[ScheduledResult]:
        output: list[ScheduledResult] = []
        parallel_batch: list[tuple[int, ToolCall]] = []

        async def flush_parallel() -> None:
            if not parallel_batch:
                return
            completed = await asyncio.gather(*(self._invoke(index, call, invoke) for index, call in parallel_batch))
            output.extend(completed)
            parallel_batch.clear()

        for index, call in enumerate(calls):
            name = str(((call.get("function") or {}).get("name")) or "")
            policy = concurrency_policy(name)
            if policy == "parallel_safe":
                parallel_batch.append((index, call))
                continue
            await flush_parallel()
            output.append(await self._invoke(index, call, invoke))
        await flush_parallel()
        return sorted(output, key=lambda item: item.index)
