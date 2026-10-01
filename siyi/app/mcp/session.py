from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from app.mcp.discovery import take_discovery_issues
from app.mcp.rpc import McpError, McpRouteRevokedError


def server_set_key(servers: list[dict[str, Any]], allow_local: bool) -> str:
    # Private runtime objects are never part of persisted server rows, but strip
    # them defensively so cache identities remain deterministic and secret-free.
    public_servers = [
        {key: value for key, value in server.items() if not str(key).startswith("_")}
        for server in servers
    ]
    payload = json.dumps(
        {"allow_local": allow_local, "servers": public_servers},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class McpSessionLease:
    """Revocable route authority and resource owner, independent of tools/list.

    Closing an idle connection does not revoke an authorized route. A management
    mutation does: even copies held by a running task retain this same lease and
    can never reopen it. Ownership starts before discovery is published.
    """

    def __init__(self) -> None:
        self.revoked = False
        self.loop = asyncio.get_running_loop()
        self._resources: dict[str, Any] = {}
        self._close_lock = asyncio.Lock()

    def check(self) -> None:
        if self.revoked:
            raise McpRouteRevokedError("MCP 配置授权已撤销，请重新发现已启用的服务")

    def resource(self, server_id: str) -> Any | None:
        self.check()
        return self._resources.get(server_id)

    def track(self, server_id: str, transport: Any) -> None:
        # Record first so a resource returned by a racing initialize is closed
        # even when invalidate happened during that await.
        self._resources[server_id] = transport
        transport.authorization_check = self.check
        self.check()

    async def close(self) -> None:
        async with self._close_lock:
            for transport in list(self._resources.values()):
                try:
                    await transport.close()
                except (OSError, RuntimeError, McpError):
                    continue
            if self.revoked:
                self._resources.clear()

    def revoke(self) -> None:
        self.revoked = True
        # A sync FastAPI management route can run on a worker thread. Never use
        # asyncio.run to close a client belonging to the application event loop.
        if self.loop.is_running() and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self.close()))


class MCPConnectionManager:
    """Caches initialized HTTP/stdio MCP sessions and closes them on eviction."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, dict[str, Any]] = {}
        self._leases: dict[str, McpSessionLease] = {}
        self._retired: list[McpSessionLease] = []
        self._generation = 0
        self._lock = asyncio.Lock()
        self._last_errors: list[dict[str, Any]] = []

    def bind_route(self, server: dict[str, Any], tool_name: str, allow_local: bool) -> tuple[dict[str, Any], str]:
        """Bind a directly requested RPC without starting a service before approval."""
        key = server_set_key([server], allow_local)
        lease = self._leases.get(key)
        if lease is None or lease.revoked:
            lease = McpSessionLease()
            self._leases[key] = lease
        return ({**server, "_mcp_lease": lease, "_session_cache_key": key}, tool_name)

    async def discover(
        self,
        servers: list[dict[str, Any]],
        allow_local: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[dict[str, Any], str]]]:
        generation = self._generation
        key = server_set_key(servers, allow_local)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and float(cached["expires_at"]) > now:
            return list(cached["definitions"]), dict(cached["routes"])
        async with self._lock:
            await self._prune(time.monotonic())
            if generation != self._generation:
                raise McpRouteRevokedError("MCP 配置在发现排队期间已变化，请重新读取配置")
            cached = self._cache.get(key)
            if cached and float(cached["expires_at"]) > time.monotonic():
                return list(cached["definitions"]), dict(cached["routes"])
            # Lazy import preserves the app.tools.mcp monkeypatch seam used by
            # compatibility tests and downstream integrations.
            from app.tools.mcp import _discover_mcp_tools_uncached

            lease = self._leases.get(key)
            if lease is None or lease.revoked:
                lease = McpSessionLease()
                self._leases[key] = lease
            configured = [{**server, "_mcp_lease": lease} for server in servers]
            try:
                definitions, routes = await _discover_mcp_tools_uncached(configured, allow_local, cache_key=key)
                lease.check()
                if generation != self._generation:
                    raise McpRouteRevokedError("MCP 配置在发现期间已变化，请重新读取配置")
            except BaseException:
                self._last_errors = take_discovery_issues(key)
                lease.revoked = True
                await lease.close()
                if self._leases.get(key) is lease:
                    self._leases.pop(key, None)
                raise
            issues = take_discovery_issues(key)
            self._last_errors = issues
            stamp = time.monotonic()
            self._cache[key] = {
                "definitions": definitions,
                "routes": routes,
                "created_at": stamp,
                "expires_at": stamp + self.ttl_seconds,
                "server_count": len(servers),
                "discovery_errors": issues,
            }
            return list(definitions), dict(routes)

    def invalidate(self, key: str | None = None) -> None:
        self._generation += 1
        if key is None:
            self._cache.clear()
            leases = list(self._leases.values())
            self._leases.clear()
        else:
            self._cache.pop(key, None)
            lease = self._leases.pop(key, None)
            leases = [lease] if lease is not None else []
        for lease in leases:
            lease.revoke()
            self._retired.append(lease)

    async def aclose(self) -> None:
        self.invalidate()
        retired, self._retired = self._retired, []
        current_loop = asyncio.get_running_loop()
        for lease in retired:
            if lease.loop is current_loop or not lease.loop.is_running():
                await lease.close()
            else:
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(lease.close(), lease.loop))

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        active = [entry for entry in self._cache.values() if float(entry["expires_at"]) > now]
        return {
            "active_session_sets": len(active),
            "cached_tools": sum(len(entry["definitions"]) for entry in active),
            "ttl_seconds": self.ttl_seconds,
            "last_discovery_errors": list(self._last_errors),
        }

    async def _prune(self, now: float) -> None:
        expired = [key for key, entry in self._cache.items() if float(entry["expires_at"]) <= now]
        for key in expired:
            self._cache.pop(key, None)
            lease = self._leases.get(key)
            if lease is not None:
                # Recycling a connection does not revoke a running task. The
                # same tracked transport may initialize again under its lease.
                await lease.close()
