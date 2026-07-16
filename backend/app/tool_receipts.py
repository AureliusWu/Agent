from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ToolReceipt:
    tool: str
    success: bool
    status: str
    changed_files: tuple[str, ...]
    exit_code: int | None
    artifact_id: str | None
    total_bytes: int | None
    truncated: bool
    error_code: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_tool_receipt(tool: str, result: dict[str, Any]) -> ToolReceipt:
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    changed: list[str] = []
    for key in ("path", "destination"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value not in changed:
            changed.append(value)
    return ToolReceipt(
        tool=tool,
        success=bool(result.get("success")),
        status=str(result.get("status") or ("ok" if result.get("success") else "error")),
        changed_files=tuple(changed),
        exit_code=int(data["exit_code"]) if isinstance(data, dict) and data.get("exit_code") is not None else None,
        artifact_id=str(data["artifact_id"]) if isinstance(data, dict) and data.get("artifact_id") else None,
        total_bytes=int(data["total_bytes"]) if isinstance(data, dict) and data.get("total_bytes") is not None else None,
        truncated=bool(result.get("truncated") or result.get("_truncated")),
        error_code=str(result["error_code"]) if result.get("error_code") else None,
    )
