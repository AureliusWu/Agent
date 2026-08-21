from __future__ import annotations

from app.providers.configuration import ProviderConfiguration
from app.security.local_only import (
    is_loopback_http_url,
    local_only_policy,
    mcp_route_is_external,
    mcp_server_is_external,
)


def test_ollama_is_the_authoritative_local_only_provider() -> None:
    assert local_only_policy(ProviderConfiguration(provider_id="ollama")).enabled is True
    assert local_only_policy(ProviderConfiguration(provider_id="deepseek")).enabled is False
    assert local_only_policy(ProviderConfiguration(provider_id="mock")).enabled is False


def test_local_only_mcp_boundary_allows_only_stdio_or_literal_loopback() -> None:
    assert is_loopback_http_url("http://127.0.0.1:9000/mcp") is True
    assert is_loopback_http_url("https://[::1]:9443/mcp") is True
    assert is_loopback_http_url("http://localhost:9000/mcp") is True
    assert is_loopback_http_url("https://localhost.example/mcp") is False
    assert mcp_server_is_external({"transport": "stdio", "command": "local.exe"}) is False
    assert mcp_server_is_external({"transport": "http", "url": "http://127.0.0.1:9000/mcp"}) is False
    assert mcp_server_is_external({"transport": "sse", "url": "https://mcp.example/mcp"}) is True
    assert mcp_server_is_external({"transport": "http", "url": "not-a-url"}) is True
    assert mcp_route_is_external(({"transport": "http", "url": "https://mcp.example"}, "send")) is True
    assert mcp_route_is_external(({"transport": "stdio", "command": "local.exe"}, "send")) is False
