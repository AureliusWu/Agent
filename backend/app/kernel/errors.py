from __future__ import annotations

from typing import Any


class KernelError(RuntimeError):
    """Stable error envelope shared by trusted kernel adapters."""

    def __init__(
        self,
        message: str,
        code: str = "kernel_error",
        *,
        component: str = "kernel",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.component = component
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "component": self.component,
            "message": str(self),
            "retryable": self.retryable,
            "details": self.details,
        }


class KernelContractError(KernelError):
    def __init__(self, message: str, *, component: str = "contracts", details: dict[str, Any] | None = None) -> None:
        super().__init__(message, "contract_violation", component=component, details=details)
