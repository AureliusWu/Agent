from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal


CancellationScope = Literal["task", "model", "tool", "child_agent", "mcp"]


@dataclass
class CancellationToken:
    scope: CancellationScope
    identifier: str
    parent: "CancellationToken | None" = None
    reason: str | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _children: list["CancellationToken"] = field(default_factory=list, init=False, repr=False)

    def child(self, scope: CancellationScope, identifier: str) -> "CancellationToken":
        token = CancellationToken(scope=scope, identifier=identifier, parent=self)
        self._children.append(token)
        if self.cancelled:
            token.cancel(self.reason or "parent_cancelled")
        return token

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str) -> None:
        if self.cancelled:
            return
        self.reason = reason
        self._event.set()
        for child in tuple(self._children):
            child.cancel(reason)

    async def wait(self) -> str:
        await self._event.wait()
        return self.reason or "cancelled"

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError(self.reason or "cancelled")


_TASK_TOKENS: dict[str, CancellationToken] = {}


def task_token(task_id: str, *, create: bool = True) -> CancellationToken | None:
    token = _TASK_TOKENS.get(task_id)
    if token is None and create:
        token = CancellationToken(scope="task", identifier=task_id)
        _TASK_TOKENS[task_id] = token
    return token


def cancel_task_token(task_id: str, reason: str) -> bool:
    token = task_token(task_id, create=False)
    if token is None:
        return False
    token.cancel(reason)
    return True


def release_task_token(task_id: str) -> None:
    _TASK_TOKENS.pop(task_id, None)
