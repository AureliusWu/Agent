from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from .database import audit
from app.security.trust import redact_payload


HookPoint = Literal["pre_tool", "post_tool", "pre_complete", "post_complete", "pre_compact", "post_compact"]
HookCallback = Callable[["HookEvent"], dict[str, Any] | None | Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class HookEvent:
    point: HookPoint
    conversation_id: int
    task_id: str
    payload: dict[str, Any]


_HOOKS: dict[HookPoint, dict[str, HookCallback]] = {
    point: {} for point in ("pre_tool", "post_tool", "pre_complete", "post_complete", "pre_compact", "post_compact")
}


def register_hook(point: HookPoint, name: str, callback: HookCallback) -> None:
    if not name or len(name) > 100:
        raise ValueError("Hook name must contain 1-100 characters")
    _HOOKS[point][name] = callback


def unregister_hook(point: HookPoint, name: str) -> None:
    _HOOKS[point].pop(name, None)


def hook_catalog() -> dict[str, list[str]]:
    return {point: sorted(callbacks) for point, callbacks in _HOOKS.items()}


async def run_hooks(event: HookEvent) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for name, callback in tuple(_HOOKS[event.point].items()):
        try:
            safe_payload, _ = redact_payload(deepcopy(event.payload))
            isolated_event = HookEvent(event.point, event.conversation_id, event.task_id, safe_payload)
            value = callback(isolated_event)
            if inspect.isawaitable(value):
                value = await value
            safe_value, _ = redact_payload(value or {})
            result = {"name": name, "status": "ok", "result": safe_value}
            audit(event.conversation_id, f"hook:{event.point}", name, "ok", {"task_id": event.task_id})
        except Exception as exc:  # Hooks are observers and cannot break the task runtime.
            result = {"name": name, "status": "error", "error": type(exc).__name__}
            audit(event.conversation_id, f"hook:{event.point}", name, "error", {"task_id": event.task_id, "error": type(exc).__name__})
        outcomes.append(result)
    return outcomes
