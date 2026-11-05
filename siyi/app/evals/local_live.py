"""Explicit local Eval configuration and read-only resident identity checks.

No service/model startup, cloud credentials, downloads or fallback. The caller
uses a dedicated Eval process and its existing temporary Runtime stage.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from app.config import settings
from app.providers.configuration import ProviderConfiguration, load_provider_configuration, validate_provider_configuration
from app.providers.capabilities import provider_identity as endpoint_identity
from app.providers.effective_capabilities import configuration_identity, context_profile_key, resolve_effective_capabilities
from app.providers.ollama import OllamaProvider
from .contracts import digest

PROTOCOL = "eval-local-live-v1"
MAX_CONFIGURATION_BYTES = 16 * 1024
SNAPSHOT_FIELDS = {"provider_id", "endpoint_hash", "endpoint_sha256", "model", "configuration_hash", "identity_hash", "model_digest",
                   "deployment_hash", "context_window_tokens", "runtime_context_window", "capabilities_sha256"}
META_FIELDS = {"protocol", "configuration_sha256", "context_window_tokens", "before", "after"}
CAPABILITY_FIELDS = ("declared_capabilities", "observed_capabilities", "explicit_profile")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate local Eval configuration field")
        result[key] = value
    return result


def _endpoint_digest(config: ProviderConfiguration) -> str:
    endpoint = urlsplit(config.base_url)
    return digest((endpoint.scheme.lower(), (endpoint.hostname or "").lower(), endpoint.port, endpoint.path.rstrip("/")))


def _ordinary(path: Path) -> os.stat_result:
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Local Eval configuration links are not allowed")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_CONFIGURATION_BYTES:
        raise ValueError("Local Eval configuration must be one bounded ordinary file")
    return info


def read_configuration(path: str | Path) -> tuple[ProviderConfiguration, str]:
    path = Path(path).absolute()
    before = _ordinary(path)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        block = stream.read(MAX_CONFIGURATION_BYTES + 1)
    if not 0 < len(block) <= MAX_CONFIGURATION_BYTES:
        raise ValueError("Local Eval configuration exceeded its bounded reader")
    after = _ordinary(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
            opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
        raise ValueError("Local Eval configuration changed during capture")
    try:
        payload = json.loads(block.decode("utf-8"), object_pairs_hook=_json_object)
        fields = ProviderConfiguration.__dataclass_fields__
        if not isinstance(payload, dict) or set(payload) - set(fields):
            raise ValueError("Local Eval configuration has unknown fields")
        config = ProviderConfiguration(**payload)
        if (any(type(getattr(config, key)) is not int for key in ("timeout_seconds", "max_tokens", "max_retries"))
                or any(type(getattr(config, key)) is not bool for key in ("allow_tools", "allow_streaming"))
                or any(type(getattr(config, key)) is not str for key in ("provider_id", "base_url", "model"))):
            raise ValueError("Local Eval configuration types differ")
        config = validate_provider_configuration(config)
        parsed = urlsplit(config.base_url)
        if config.provider_id != "ollama" or not config.base_url or parsed.path not in {"", "/"}:
            raise ValueError("Explicit local Eval only supports the original local Ollama boundary")
    except (UnicodeError, TypeError, json.JSONDecodeError):
        raise ValueError("Invalid local Eval configuration") from None
    return config, hashlib.sha256(block).hexdigest()


@contextmanager
def isolated_configuration(config: ProviderConfiguration, context_window: int, stage: Path):
    if type(context_window) is not int or not 1 <= context_window <= 10_000_000:
        raise ValueError("Explicit local Eval requires a positive deployed context window")
    owned = stage / "local-provider.json"
    with owned.open("x", encoding="utf-8") as stream:
        json.dump(asdict(config), stream, ensure_ascii=False)
    names = ("AGENT_PROVIDER_CONFIG_PATH", "AGENT_MODEL_CONTEXT_PROFILES_JSON", "SIYI_ALLOW_PAID_API")
    previous = {name: os.environ.get(name) for name in names}
    previous_profile = settings.model_context_profiles_json
    os.environ["AGENT_PROVIDER_CONFIG_PATH"] = str(owned)
    settings.model_context_profiles_json = json.dumps({context_profile_key(config): {"context_window_tokens": context_window}})
    os.environ["AGENT_MODEL_CONTEXT_PROFILES_JSON"] = settings.model_context_profiles_json
    os.environ["SIYI_ALLOW_PAID_API"] = "false"
    try:
        yield
    finally:
        settings.model_context_profiles_json = previous_profile
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def resident_snapshot(config: ProviderConfiguration, context_window: int) -> dict:
    """Metadata only; the original resolver independently checks Windows PID/lifetime."""
    if load_provider_configuration() != config:
        raise ValueError("Selected local Eval configuration drifted")
    try:
        metadata = await asyncio.wait_for(OllamaProvider(config).diagnostics(), timeout=min(config.timeout_seconds, 45))
    except Exception:
        raise ValueError("Local Eval metadata refresh failed; no fallback") from None
    if not isinstance(metadata, dict) or metadata.get("status") != "ok":
        raise ValueError("Local Eval diagnostics are not ready")
    current = resolve_effective_capabilities(configuration=config).public()
    if (load_provider_configuration() != config or current.get("provider_id") != "ollama"
            or current.get("local") is not True or current.get("observation_stale") is not False
            or current.get("endpoint_hash") != endpoint_identity(config.base_url)[1]
            or current.get("model") != config.model or current.get("configuration_hash") != configuration_identity(config)
            or type(current.get("context_window_tokens")) is not int or current["context_window_tokens"] != context_window
            or type(current.get("runtime_context_window")) is not int or current["runtime_context_window"] != context_window):
        raise ValueError("Local Eval resident configuration or context is unknown or changed")
    snapshot = {key: current.get(key) for key in SNAPSHOT_FIELDS - {"capabilities_sha256", "endpoint_sha256"}}
    snapshot["endpoint_sha256"] = _endpoint_digest(config)
    snapshot["capabilities_sha256"] = capabilities_digest(current)
    if snapshot_errors(snapshot):
        raise ValueError("Local Eval resident identity is incomplete")
    return snapshot


def snapshot_errors(value) -> list[str]:
    if not isinstance(value, dict) or set(value) != SNAPSHOT_FIELDS:
        return ["local_live_snapshot_fields"]
    if value["provider_id"] != "ollama" or not isinstance(value["model"], str) or not value["model"]:
        return ["local_live_provider"]
    if not isinstance(value["endpoint_hash"], str) or not re.fullmatch(r"[a-f0-9]{16}", value["endpoint_hash"]):
        return ["local_live_endpoint"]
    if any(not isinstance(value[key], str) or not _SHA.fullmatch(value[key]) for key in (
            "endpoint_sha256", "model_digest", "configuration_hash", "identity_hash", "deployment_hash", "capabilities_sha256")):
        return ["local_live_identity"]
    if (type(value["context_window_tokens"]) is not int or not 1 <= value["context_window_tokens"] <= 10_000_000
            or type(value["runtime_context_window"]) is not int
            or value["runtime_context_window"] != value["context_window_tokens"]):
        return ["local_live_context"]
    return []


def public_provider(config: ProviderConfiguration, configuration_sha256: str, context_window: int,
                    before: dict | None, after: dict | None) -> dict:
    return {"endpoint_sha256": _endpoint_digest(config), "model": config.model,
            "local_live": {"protocol": PROTOCOL, "configuration_sha256": configuration_sha256,
                           "context_window_tokens": context_window, "before": before, "after": after}}


def provider_errors(provider: dict) -> list[str]:
    """Strict public facts for the existing consumer; never treats metadata as execution."""
    if "local_live" not in provider:
        return []
    if set(provider) != {"endpoint_sha256", "model", "local_live"}:
        return ["local_live_provider_fields"]
    value = provider["local_live"]
    if not isinstance(value, dict) or set(value) != META_FIELDS or value["protocol"] != PROTOCOL:
        return ["local_live_protocol"]
    if (not isinstance(value["configuration_sha256"], str) or not _SHA.fullmatch(value["configuration_sha256"])
            or type(value["context_window_tokens"]) is not int):
        return ["local_live_configuration"]
    errors = snapshot_errors(value["before"]) + snapshot_errors(value["after"])
    if errors:
        return errors
    if (value["before"] != value["after"] or value["before"]["model"] != provider["model"]
            or value["before"]["endpoint_sha256"] != provider["endpoint_sha256"]
            or value["before"]["context_window_tokens"] != value["context_window_tokens"]):
        return ["local_live_identity_changed"]
    return []


def capabilities_digest(identity: dict) -> str:
    if any(not isinstance(identity.get(key), dict) for key in CAPABILITY_FIELDS):
        raise ValueError("Local Eval capability fields are missing")
    return digest({key: identity[key] for key in CAPABILITY_FIELDS})


def designated_provider_errors(provider: dict, designated: dict) -> list[str]:
    """Bind the formal RC core family to its separately designated live model.

    A comparable pair alone is insufficient: two reports can agree with each
    other while describing different weights or a different listener lifetime
    from the Runtime file qualification in the same bundle.
    """
    if (not isinstance(designated, dict) or designated.get("provider_id") != "ollama"
            or designated.get("local") is not True or designated.get("observation_stale") is not False):
        return ["local_live_designated_identity_required"]
    if "local_live" not in provider:
        return ["local_live_designated_metadata_missing"]
    errors = provider_errors(provider)
    if errors:
        return errors
    try:
        expected_capabilities = capabilities_digest(designated)
    except (ValueError, TypeError):
        return ["local_live_designated_capabilities_missing"]
    # EffectiveCapabilities.public() redacts the endpoint itself. Its original
    # endpoint_hash is available and mandatory; no raw URL is reconstructed.
    fields = ("provider_id", "endpoint_hash", "model", "configuration_hash", "identity_hash", "model_digest",
              "deployment_hash", "context_window_tokens", "runtime_context_window")
    for phase in ("before", "after"):
        observed = provider["local_live"][phase]
        for key in fields:
            if key not in designated or type(designated[key]) is not type(observed[key]) or designated[key] != observed[key]:
                errors.append("local_live_designated_mismatch:" + key)
        if observed["capabilities_sha256"] != expected_capabilities:
            errors.append("local_live_designated_mismatch:capabilities")
        if "endpoint_sha256" in designated and designated["endpoint_sha256"] != observed["endpoint_sha256"]:
            errors.append("local_live_designated_mismatch:endpoint_sha256")
    return list(dict.fromkeys(errors))
