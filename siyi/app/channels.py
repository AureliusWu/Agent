from __future__ import annotations

from typing import Any, Protocol


class InputChannel(Protocol):
    async def receive(self) -> str | None: ...


class OutputChannel(Protocol):
    async def send(self, content: str) -> None: ...


class ComputerUseProvider(Protocol):
    @property
    def available(self) -> bool: ...

    async def execute(self, action: dict[str, Any]) -> dict[str, Any]: ...


class DisabledComputerUseProvider:
    available = False

    async def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        return {"success": False, "status": "unavailable", "error_code": "computer_use_disabled"}
