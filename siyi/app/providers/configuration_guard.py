"""Bind saved-configuration requests to a credential audience during updates.

The lock is only held while checking/registering requests or replacing config;
it must never remain held while an async model request is in flight.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from contextlib import contextmanager
from threading import RLock
from typing import Iterator

from app.providers.configuration import ProviderConfiguration, load_provider_configuration, provider_credential_identity
from app.providers.descriptors import descriptor_for_configuration


_configuration_lock = RLock()
_inflight_requests = 0


class CredentialTargetConflict(RuntimeError):
    pass


def model_credential_target(configuration: ProviderConfiguration) -> str:
    descriptor = descriptor_for_configuration(configuration)
    if descriptor.credential_policy == "forbidden":
        destination = "local:no-cloud-credentials"
    else:
        provider, endpoint = provider_credential_identity(configuration)
        destination = f"{provider}:{endpoint}"
    return "model_api_key." + hashlib.sha256(destination.encode("utf-8")).hexdigest()


def validate_credential_target(configuration: ProviderConfiguration, target: str | None) -> None:
    if target is None:
        return
    try:
        expected = model_credential_target(configuration)
    except ValueError:
        raise CredentialTargetConflict("模型目标地址无效，请核对提供商和 API 基础地址后重试。") from None
    if not re.fullmatch(r"model_api_key\.[a-f0-9]{64}", target) or not hmac.compare_digest(expected, target):
        raise CredentialTargetConflict("模型提供商配置已改变。已阻止向新地址发送旧密钥，请刷新配置后重试。")


def saved_model_request(path: str, method: str) -> bool:
    if method.upper() == "GET":
        return path == "/api/provider/health"
    if method.upper() != "POST":
        return False
    return path in {"/api/chat", "/api/tasks"} or bool(
        re.fullmatch(r"/api/tasks/[^/]+/resume", path)
        or re.fullmatch(r"/api/conversations/[^/]+/compact", path)
    )


@contextmanager
def credential_request_guard(path: str, method: str, target: str | None) -> Iterator[None]:
    global _inflight_requests
    protected = saved_model_request(path, method)
    if protected:
        with _configuration_lock:
            if target is not None:
                validate_credential_target(load_provider_configuration(), target)
            _inflight_requests += 1
    try:
        yield
    finally:
        if protected:
            with _configuration_lock:
                _inflight_requests -= 1


@contextmanager
def configuration_update_guard() -> Iterator[int]:
    with _configuration_lock:
        yield _inflight_requests
