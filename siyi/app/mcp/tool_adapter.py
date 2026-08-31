from __future__ import annotations

from typing import Any

from app.mcp.rpc import McpError


def typed_tool_failure(error: McpError, *, snapshot_id: str | None = None) -> dict[str, Any]:
    """Normalize protocol/transport failures for context, receipts, and audit."""

    return error.to_tool_failure(snapshot_id=snapshot_id)
