"""Credential-safe, non-generating diagnostics for a Provider settings draft.

Model discovery is deliberately not a chat/capability test: it only reads the
provider's catalogue and never persists either the draft or its credentials.
"""
from __future__ import annotations

import socket
import ssl
import time
from dataclasses import replace
from typing import Any

import httpx

from app.providers.configuration import ProviderConfiguration, _validate_model_id, validate_provider_configuration
from app.providers.descriptors import descriptor_for_configuration
from app.providers.provider import _provider_endpoint, _resolve_api_key
from app.security.network_security import NetworkPolicyError, guarded_request
from app.security.trust import redact_payload


def connection_error(error: Exception) -> tuple[str, str]:
    """Classify failures without returning arbitrary transport/credential text."""
    causes: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(causes) < 8 and current not in causes:
        causes.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, FileNotFoundError) for item in causes):
        return "runtime_file_missing", "程序缺少 HTTPS 运行文件，请更新完整司忆程序；重新填写 API Key 无法解决。"
    if any(isinstance(item, socket.gaierror) for item in causes):
        return "dns_error", "无法解析 API 地址，请检查网络、DNS 和 API 地址。"
    if any(isinstance(item, ssl.SSLError) for item in causes) or any(
        "certificate verify failed" in str(item).lower() for item in causes
    ):
        return "tls_error", "HTTPS 证书验证失败，请检查系统时间、代理和证书配置；不会跳过证书校验。"
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        messages = {
            401: ("authentication", "API Key 无效或已失效，请检查该提供商的密钥。"),
            402: ("quota_exhausted", "模型服务余额或配额不足，请检查提供商账户。"),
            403: ("access_denied", "该 API Key 无权访问此服务，请检查提供商账户和访问权限。"),
            404: ("endpoint_not_found", "未找到模型目录接口，请检查 API 基础地址，不要填写完整 chat/completions 地址。"),
            429: ("rate_limited", "模型服务暂时限流，请稍后手动重试。"),
        }
        if status in messages:
            return messages[status]
        if 300 <= status < 400:
            return "redirect_blocked", "API 地址返回重定向，已阻止继续发送凭据；请填写最终的 API 基础地址。"
        if status >= 500:
            return "service_unavailable", "模型服务暂时不可用，请稍后手动重试。"
        return "http_error", f"模型目录请求失败（HTTP {status}），请检查 API 地址和提供商配置。"
    if isinstance(error, NetworkPolicyError):
        if "cannot resolve" in str(error).lower():
            return "dns_error", "无法解析 API 地址，请检查网络、DNS 和 API 地址。"
        if "redirect" in str(error).lower():
            return "redirect_blocked", "API 地址返回重定向，已阻止继续发送凭据；请填写最终的 API 基础地址。"
        return "network_policy", "网络安全策略阻止了该 API 地址或重定向，请检查提供商地址。"
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "timeout", "连接模型服务超时，请检查网络、代理和 API 地址。"
    if isinstance(error, httpx.ConnectError):
        return "connection_error", "无法连接模型服务，请检查网络、代理和服务是否正在运行。"
    if isinstance(error, (httpx.TransportError, OSError)):
        return "network_error", "模型服务网络连接失败，请检查网络与代理配置。"
    if isinstance(error, ValueError):
        return "invalid_response", "服务返回的模型目录格式不正确，未能读取可用模型。"
    return "diagnostic_error", "无法完成连接检查，请查看本机诊断状态或更新完整司忆程序。"


def _catalogue(payload: Any, *, ollama: bool, secret: str) -> tuple[list[dict[str, str]], bool]:
    raw_models = payload.get("models" if ollama else "data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raise ValueError("invalid model catalogue")
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_models[:512]:
        if not isinstance(item, dict):
            continue
        identifier = (item.get("name") or item.get("model")) if ollama else item.get("id")
        if not isinstance(identifier, str):
            continue
        try:
            _validate_model_id(identifier)
        except ValueError:
            continue
        redacted, _ = redact_payload(identifier)
        if identifier in seen or redacted != identifier or (len(secret) >= 8 and secret in identifier):
            continue
        name = item.get("name") if not ollama else identifier
        name = name if isinstance(name, str) else identifier
        name = "".join(char for char in name[:160] if ord(char) >= 32 and ord(char) != 127)
        name, _ = redact_payload(name)
        if len(secret) >= 8:
            name = name.replace(secret, "***REDACTED***")
        models.append({"id": identifier, "name": name or identifier})
        seen.add(identifier)
    return models, len(raw_models) > 512


async def discover_models(configuration: ProviderConfiguration, api_key: str | None = None) -> dict[str, Any]:
    # Discovery precedes selecting a model. Validate the entire draft without
    # requiring a selected model for local/generic services solely for this GET.
    validation_config = configuration
    if configuration.provider_id in {"ollama", "openai_compatible"} and not configuration.model:
        validation_config = replace(configuration, model="model-discovery")
    validated = validate_provider_configuration(validation_config)
    descriptor = descriptor_for_configuration(validated)
    if descriptor.provider_id == "deepseek":
        # Defaults resolved from a legacy environment must obey the same
        # official-endpoint policy as fields explicitly entered in this draft.
        validate_provider_configuration(replace(validated, base_url=descriptor.endpoint, model=descriptor.model))
    selected_model = configuration.model or (descriptor.model if configuration.provider_id == "deepseek" else "")
    result: dict[str, Any] = {
        "status": "ok", "provider": descriptor.provider_id, "model": selected_model,
        "latency_ms": None, "models": [], "selected_model_available": None,
        "connection_scope": "model_catalog_only", "chat_verified": False,
    }
    if descriptor.provider_id == "mock":
        result["models"] = [{"id": "siyi-mock-v1", "name": "Deterministic Mock"}]
        result["selected_model_available"] = selected_model == "siyi-mock-v1" if selected_model else None
        return result
    key = _resolve_api_key(api_key, descriptor.endpoint, descriptor.credential_policy)
    if descriptor.credential_policy == "required" and not key:
        return {**result, "status": "unconfigured", "error_type": "missing_api_key", "error": "请填写 API Key，或使用已保存在 Windows 凭据管理器中的密钥。"}
    started = time.perf_counter()
    try:
        endpoint = (f"{descriptor.endpoint}/api/tags" if descriptor.provider_id == "ollama"
                    else _provider_endpoint(descriptor.endpoint, "models"))
        async with httpx.AsyncClient(timeout=min(descriptor.timeout, 30), follow_redirects=False) as client:
            response = await guarded_request(
                client, "GET", endpoint, purpose="model_provider_catalogue",
                headers={"Authorization": f"Bearer {key}"} if key else {},
                allow_private=descriptor.local,
                allow_redirects=False,
            )
            response.raise_for_status()
            models, truncated = _catalogue(response.json(), ollama=descriptor.provider_id == "ollama", secret=key)
        selected_available = None
        if selected_model:
            selected_available = True if selected_model in {item["id"] for item in models} else None if truncated else False
        return {
            **result, "latency_ms": round((time.perf_counter() - started) * 1000),
            "models": models, "truncated": truncated,
            "selected_model_available": selected_available,
        }
    except Exception as error:
        error_type, message = connection_error(error)
        return {**result, "status": "error", "error_type": error_type, "error": message,
                "http_status": error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None,
                "latency_ms": round((time.perf_counter() - started) * 1000)}
