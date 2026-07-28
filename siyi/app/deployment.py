from __future__ import annotations

import ipaddress
from typing import Any

from .config import Settings, settings


class DeploymentSecurityError(RuntimeError):
    pass


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def deployment_requires_auth(configuration: Settings = settings) -> bool:
    return False


def validate_deployment_security(configuration: Settings = settings) -> dict[str, Any]:
    loopback = is_loopback_host(configuration.bind_host)
    if not loopback:
        raise DeploymentSecurityError("desktop_local 只能监听回环地址")
    return {
        "mode": configuration.deployment_mode,
        "bind_host": configuration.bind_host,
        "loopback": loopback,
        "authentication_required": False,
        "authentication_configured": bool(configuration.api_token.strip()),
    }
