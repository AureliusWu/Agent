from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .config import settings


REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "mcp-session-id", "x-api-key"}
METADATA_HOSTS = {
    "169.254.169.254",
    "100.100.100.200",
    "metadata.google.internal",
    "metadata.azure.internal",
    "metadata.aws.internal",
}


class NetworkPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.host, self.port


def _domain_matches(host: str, pattern: str) -> bool:
    normalized = pattern.strip().lower().rstrip(".")
    if not normalized:
        return False
    if normalized.startswith("*."):
        suffix = normalized[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return host == normalized


async def _resolve_host(host: str, port: int) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
        return (literal,)
    except ValueError:
        pass

    def resolve() -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple({ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos})

    try:
        return await asyncio.wait_for(asyncio.to_thread(resolve), timeout=5)
    except (OSError, TimeoutError) as exc:
        raise NetworkPolicyError(f"Cannot resolve outbound host: {host}") from exc


def _address_allowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_private: bool) -> bool:
    text = str(address)
    if text in METADATA_HOSTS or address.is_link_local:
        return False
    if address.is_loopback or address.is_private:
        return allow_private
    return not (address.is_multicast or address.is_reserved or address.is_unspecified)


async def validate_outbound_url(
    url: str,
    *,
    purpose: str,
    allow_private: bool = False,
    allowed_domains: tuple[str, ...] | None = None,
) -> ValidatedUrl:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise NetworkPolicyError(f"Unsupported outbound URL scheme for {purpose}")
    if parsed.username or parsed.password:
        raise NetworkPolicyError("Credentials in outbound URLs are forbidden")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise NetworkPolicyError("Outbound URL is missing a host")
    if host in METADATA_HOSTS or host == "localhost" or host.endswith(".localhost"):
        if host in METADATA_HOSTS or not allow_private:
            raise NetworkPolicyError(f"Blocked outbound host: {host}")
    if any(_domain_matches(host, item) for item in settings.network_blocked_domain_list):
        raise NetworkPolicyError(f"Blocked outbound domain: {host}")
    effective_allowlist = allowed_domains if allowed_domains is not None else settings.network_allowed_domain_list
    if effective_allowlist and not any(_domain_matches(host, item) for item in effective_allowlist):
        raise NetworkPolicyError(f"Outbound domain is not allowlisted: {host}")
    port = parsed.port or (443 if scheme == "https" else 80)
    addresses = await _resolve_host(host, port)
    if not addresses or any(not _address_allowed(address, allow_private) for address in addresses):
        raise NetworkPolicyError(f"Outbound address is private, reserved, or unsafe: {host}")
    has_public_address = any(not (address.is_loopback or address.is_private) for address in addresses)
    if scheme == "http" and not settings.network_allow_http and (not allow_private or has_public_address):
        raise NetworkPolicyError("Plain HTTP is disabled for public outbound requests")
    return ValidatedUrl(url, scheme, host, port, tuple(sorted(str(item) for item in addresses)))


def _response_headers(response: Any) -> dict[str, str]:
    headers = getattr(response, "headers", {}) or {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _response_size(response: Any) -> int:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return len(content)
    text = getattr(response, "text", None)
    return len(text.encode("utf-8")) if isinstance(text, str) else 0


async def guarded_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    purpose: str,
    headers: dict[str, str] | None = None,
    json: Any = None,
    allow_private: bool = False,
    allowed_domains: tuple[str, ...] | None = None,
    max_response_bytes: int | None = None,
) -> httpx.Response:
    normalized_method = method.upper()
    if normalized_method not in {"GET", "POST"}:
        raise NetworkPolicyError(f"Outbound HTTP method is not allowed: {normalized_method}")
    max_bytes = max_response_bytes or settings.network_max_response_bytes
    current_url = url
    current_method = normalized_method
    current_json = json
    request_headers = dict(headers or {})
    previous: ValidatedUrl | None = None
    for redirect_count in range(settings.network_max_redirects + 1):
        validated = await validate_outbound_url(
            current_url,
            purpose=purpose,
            allow_private=allow_private,
            allowed_domains=allowed_domains,
        )
        if previous and previous.origin != validated.origin and any(key.lower() in SENSITIVE_HEADERS for key in request_headers):
            raise NetworkPolicyError("Cross-origin redirect with credentials is forbidden")
        caller = getattr(client, current_method.lower())
        kwargs: dict[str, Any] = {"headers": request_headers}
        if current_json is not None and current_method == "POST":
            kwargs["json"] = current_json
        response = await caller(current_url, **kwargs)
        response_headers = _response_headers(response)
        if int(getattr(response, "status_code", 0)) in REDIRECT_STATUSES:
            location = response_headers.get("location")
            if not location:
                raise NetworkPolicyError("Redirect response is missing Location")
            if redirect_count >= settings.network_max_redirects:
                raise NetworkPolicyError("Outbound redirect limit exceeded")
            previous = validated
            current_url = urljoin(current_url, location)
            if int(response.status_code) == 303:
                current_method, current_json = "GET", None
            continue
        disposition = response_headers.get("content-disposition", "").lower()
        if "attachment" in disposition:
            raise NetworkPolicyError("Attachment downloads are not allowed")
        declared = response_headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise NetworkPolicyError("Outbound response exceeds the configured size limit")
        if _response_size(response) > max_bytes:
            raise NetworkPolicyError("Outbound response exceeds the configured size limit")
        return response
    raise NetworkPolicyError("Outbound redirect limit exceeded")
