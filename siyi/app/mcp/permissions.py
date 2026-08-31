from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.security.trust import redact_payload
from app.mcp.rpc import McpError


_SECRET_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "auth_token",
    "token",
    "secret",
    "client_secret",
    "password",
    "authorization",
    "credential",
}
_SECRET_BINDING = re.compile(r"^env:([A-Za-z_][A-Za-z0-9_]{0,127})$")
_STDIO_ENV_ALLOW = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}


class McpSecretBindingError(McpError):
    """Raised when plaintext credentials are supplied without a secret binding."""

    error_code = "mcp_secret_binding_required"


def _secret_name(value: str) -> bool:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return normalized in _SECRET_NAMES or normalized.endswith(
        ("_api_key", "_token", "_secret", "_password", "_credential")
    )


def normalize_secret_binding(secret_binding: str | None) -> str | None:
    if not secret_binding:
        return None
    normalized = secret_binding.strip()
    if not _SECRET_BINDING.fullmatch(normalized):
        raise McpSecretBindingError("MCP 密钥绑定必须使用 env:VARIABLE_NAME 引用，不得包含密钥值")
    return normalized


def resolve_secret_binding(secret_binding: str | None) -> tuple[str, str] | None:
    normalized = normalize_secret_binding(secret_binding)
    if normalized is None:
        return None
    name = normalized.removeprefix("env:")
    value = os.environ.get(name, "")
    if not value:
        raise McpSecretBindingError("MCP 密钥绑定引用的环境凭据不存在")
    return name, value


def stdio_environment(secret_binding: str | None = None) -> dict[str, str]:
    """Build a minimal child environment and add only an explicit secret binding."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _STDIO_ENV_ALLOW and not _secret_name(key)
    }
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUNBUFFERED", "1")
    binding = resolve_secret_binding(secret_binding)
    if binding is not None:
        name, value = binding
        environment[name] = value
    return environment


def http_binding_headers(secret_binding: str | None = None) -> dict[str, str]:
    binding = resolve_secret_binding(secret_binding)
    if binding is None:
        return {}
    _name, value = binding
    return {"Authorization": f"Bearer {value}"}


def validate_http_server_binding(url: str, *, secret_binding: str | None = None) -> None:
    normalize_secret_binding(secret_binding)
    parsed = urlsplit(url)
    has_userinfo = parsed.username is not None or parsed.password is not None
    sensitive_query = any(_secret_name(key) for key, _value in parse_qsl(parsed.query, keep_blank_values=True))
    if has_userinfo or sensitive_query:
        raise McpSecretBindingError("MCP 服务凭据必须使用专用密钥绑定，禁止嵌入 URL")


def validate_stdio_server_binding(
    command: str,
    args: list[str],
    *,
    secret_binding: str | None = None,
) -> None:
    normalize_secret_binding(secret_binding)
    _cleaned, sensitive = redact_payload({"command": command, "args": args})
    # Check the argument name before its value: --token=value, TOKEN=value,
    # /password:value and --password value are the same credential boundary.
    separate_secret = any(
        _secret_name(re.split(r"[=:]", argument.lstrip("-/"), maxsplit=1)[0])
        for argument in args
    )
    for argument in args:
        for embedded_url in re.findall(r"https?://[^\s'\"<>]+", argument, flags=re.IGNORECASE):
            validate_http_server_binding(embedded_url, secret_binding=secret_binding)
    if sensitive.redactions or separate_secret:
        raise McpSecretBindingError("stdio MCP 凭据必须使用专用密钥绑定，禁止写入命令参数")


def redact_bound_value(value: Any, secret_value: str | None) -> Any:
    """Never echo an explicitly bound value, even when it has no key prefix."""
    if not secret_value:
        return value
    if isinstance(value, str):
        return value.replace(secret_value, "[REDACTED]")
    if isinstance(value, list):
        return [redact_bound_value(item, secret_value) for item in value]
    if isinstance(value, dict):
        return {redact_bound_value(key, secret_value): redact_bound_value(item, secret_value) for key, item in value.items()}
    return value


def validate_tool_arguments(arguments: dict[str, Any]) -> tuple[int, tuple[str, ...]]:
    """Return redaction metadata so callers can fail closed before transport."""

    _cleaned, sensitive = redact_payload(arguments)
    return sensitive.redactions, sensitive.categories
