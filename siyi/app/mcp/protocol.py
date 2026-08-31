from __future__ import annotations

from typing import Any

from app import __version__


JSON_RPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2025-03-26"


def request_payload(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": JSON_RPC_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    }


def notification_payload(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": JSON_RPC_VERSION, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "AureliusWu Agent", "version": __version__},
    }
