"""Execution envelopes shared by the Executor and trusted capability plugins."""

from dataclasses import dataclass
from typing import Any

from app.tools.receipts import ToolReceipt


@dataclass(frozen=True)
class RuntimeToolOutcome:
    result: dict[str, Any]
    confirmed: bool
    risk: str
    source: str
    receipt: ToolReceipt | None = None
