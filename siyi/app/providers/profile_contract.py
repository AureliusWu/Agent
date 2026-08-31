"""Stable provider resume identity, independent of UI/health observations."""
from __future__ import annotations

from typing import Any


def _persisted_value(value: Any) -> Any:
    """Normalize JSON's tuple-to-array conversion without weakening identity."""
    if isinstance(value, dict):
        return {key: _persisted_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_persisted_value(item) for item in value]
    return value


def profiles_match_for_resume(stored: dict[str, Any], current: dict[str, Any]) -> bool:
    # v14 stored the transport profile, before descriptors were added. Compare
    # the identity fields it actually had, without treating new display fields
    # as a provider switch. Missing identity never authorizes a different one.
    identity = ("id", "request_url", "chat_endpoint", "default_model", "credential_env", "models")
    for field in identity:
        left, right = stored.get(field), current.get(field)
        if field == "models":
            left, right = sorted(left or []), sorted(right or [])
        if left != right:
            return False
    old_descriptor = stored.get("descriptor")
    new_descriptor = current.get("descriptor")
    if not isinstance(old_descriptor, dict):
        # Legacy snapshots have no allow_tools/streaming settings. Restrict
        # compatibility to the legacy default, not a changed v15 contract.
        if isinstance(new_descriptor, dict):
            caps = new_descriptor.get("capabilities") or {}
            if caps.get("native_tool_calls") is False or caps.get("streaming") is False:
                return False
        return True
    if not isinstance(new_descriptor, dict):
        return False
    stable = ("provider_id", "endpoint", "model", "credential_policy", "timeout", "retry_policy", "local")
    if any(_persisted_value(old_descriptor.get(key)) != _persisted_value(new_descriptor.get(key)) for key in stable):
        return False
    old_caps = old_descriptor.get("capabilities") or {}
    new_caps = new_descriptor.get("capabilities") or {}
    return all(
        old_caps.get(key) == new_caps.get(key)
        for key in ("streaming", "native_tool_calls", "default_max_output_tokens")
    )
