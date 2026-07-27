from __future__ import annotations

import ipaddress
from typing import Any

from .config import Settings, settings


REMOTE_DEPLOYMENT_MODES = {"web_control", "cloud_executor"}


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
    return configuration.deployment_mode in REMOTE_DEPLOYMENT_MODES or not is_loopback_host(configuration.bind_host)


def validate_deployment_security(configuration: Settings = settings) -> dict[str, Any]:
    loopback = is_loopback_host(configuration.bind_host)
    requires_auth = deployment_requires_auth(configuration)
    if configuration.deployment_mode == "desktop_local" and not loopback:
        raise DeploymentSecurityError("desktop_local 只能监听回环地址")
    if requires_auth and not configuration.api_token.strip():
        raise DeploymentSecurityError("非本地部署或非回环监听必须配置 AGENT_API_TOKEN")
    return {
        "mode": configuration.deployment_mode,
        "bind_host": configuration.bind_host,
        "loopback": loopback,
        "authentication_required": requires_auth,
        "authentication_configured": bool(configuration.api_token.strip()),
    }
