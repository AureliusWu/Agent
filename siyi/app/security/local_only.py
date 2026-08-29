from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.providers.configuration import ProviderConfiguration, endpoint_is_local, load_provider_configuration


@dataclass(frozen=True)
class LocalOnlyPolicy:
    """Authoritative outbound-network policy for the active model provider."""

    enabled: bool
    provider_id: str


def local_only_policy(config: ProviderConfiguration | None = None) -> LocalOnlyPolicy:
    """Treat an explicitly selected loopback provider as local/offline mode.

    Provider configuration is the same authority used to choose the model
    transport.  Keeping this decision here prevents UI state or prompt text
    from silently weakening the offline boundary.
    """

    active = config or load_provider_configuration()
    provider_id = active.provider_id.strip().casefold()
    compatible_local = provider_id == "openai_compatible" and endpoint_is_local(active.base_url)
    return LocalOnlyPolicy(enabled=provider_id == "ollama" or compatible_local, provider_id=provider_id)


def is_loopback_http_url(value: Any) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
        hostname = (parsed.hostname or "").strip().casefold()
        if parsed.scheme not in {"http", "https"} or not hostname:
            return False
        if hostname == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def mcp_server_is_external(server: dict[str, Any]) -> bool:
    """Return whether discovery/invocation would cross the local machine.

    Stdio MCP and loopback HTTP/SSE remain available in local-only mode.  A
    malformed network route fails closed as external.
    """

    transport = str(server.get("transport") or "").strip().casefold()
    if transport == "stdio":
        return False
    if transport in {"http", "sse"}:
        return not is_loopback_http_url(server.get("url"))
    return True


def mcp_route_is_external(route: Any) -> bool:
    try:
        server, _ = route
    except (TypeError, ValueError):
        return True
    return not isinstance(server, dict) or mcp_server_is_external(server)
