from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from app.config import settings


OLLAMA_BASE_URL = "http://127.0.0.1:11434"
# Kept only as the migration/default value for pre-v15 installations.  It is
# no longer an allow-list: every safely named installed Ollama model can be
# selected and persisted.
OLLAMA_MODEL = "qwen3:4b"
PROVIDER_IDS = {"deepseek", "ollama", "openai_compatible", "mock"}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}$")


@dataclass(frozen=True)
class ProviderConfiguration:
    provider_id: str = "deepseek"
    base_url: str = ""
    model: str = ""
    timeout_seconds: int = 90
    max_tokens: int = 8192
    max_retries: int = 2
    allow_tools: bool = True
    allow_streaming: bool = True


def provider_config_path() -> Path:
    override = os.getenv("AGENT_PROVIDER_CONFIG_PATH", "").strip()
    if override:
        return Path(override)
    return settings.database_path.parent / "state" / "provider-settings.json"


def validate_provider_configuration(config: ProviderConfiguration) -> ProviderConfiguration:
    if config.provider_id not in PROVIDER_IDS:
        raise ValueError("不支持的模型 Provider")
    if not 1 <= config.timeout_seconds <= 600:
        raise ValueError("超时必须在 1 到 600 秒之间")
    if not 1 <= config.max_tokens <= 1_000_000:
        raise ValueError("最大输出 Token 无效")
    if not 0 <= config.max_retries <= 5:
        raise ValueError("重试次数必须在 0 到 5 之间")
    if config.provider_id == "deepseek":
        if config.base_url:
            parsed = urlsplit(config.base_url)
            if (
                parsed.scheme != "https"
                or (parsed.hostname or "").casefold() != "api.deepseek.com"
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("DeepSeek Provider 只允许不含凭据的官方 HTTPS endpoint")
        if config.model:
            _validate_model_id(config.model)
    if config.provider_id == "ollama":
        parsed = urlsplit(config.base_url or OLLAMA_BASE_URL)
        if parsed.scheme != "http" or (parsed.hostname or "").lower() not in _LOCAL_HOSTS:
            raise ValueError("Ollama 只允许本机 HTTP 回环地址")
        if parsed.port != 11434 or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Ollama 只允许本机 11434 端口")
        _validate_model_id(config.model)
        if config.max_tokens < 2048:
            raise ValueError("本地模型最大输出 Token 不得低于 2048，以避免只有思考而没有正文")
    if config.provider_id == "openai_compatible":
        _validate_openai_compatible_endpoint(config.base_url)
        _validate_model_id(config.model)
    if config.provider_id == "mock" and (config.base_url or config.model):
        raise ValueError("Mock Provider 不接受 endpoint、model 或凭据配置")
    return config


def _validate_model_id(model: str) -> str:
    normalized = model.strip()
    if normalized != model or not _MODEL_ID.fullmatch(normalized) or ".." in normalized:
        raise ValueError("Provider model 必须是非空且安全的模型标识")
    return normalized


def endpoint_is_local(base_url: str) -> bool:
    return (urlsplit(base_url).hostname or "").casefold() in _LOCAL_HOSTS


def _validate_openai_compatible_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("OpenAI-compatible endpoint 必须是 HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("OpenAI-compatible endpoint 不得包含凭据、查询参数或 fragment")
    if not endpoint_is_local(base_url) and parsed.scheme != "https":
        raise ValueError("非本机 OpenAI-compatible endpoint 必须使用 HTTPS")
    return base_url.rstrip("/")


def configuration_for_provider(
    provider_id: str,
    source: ProviderConfiguration | None = None,
) -> ProviderConfiguration:
    """Return a validated, non-persistent configuration for provider diagnostics."""
    current = source or load_provider_configuration()
    if provider_id == current.provider_id:
        return current
    if provider_id == "ollama":
        return validate_provider_configuration(
            replace(
                current,
                provider_id="ollama",
                base_url=OLLAMA_BASE_URL,
                model=OLLAMA_MODEL,
                max_tokens=max(current.max_tokens, 2048),
            )
        )
    if provider_id == "openai_compatible":
        if current.provider_id == "openai_compatible":
            return validate_provider_configuration(current)
        # Preview is intentionally incomplete until the user names an
        # endpoint and model; callers can render the generic contract without
        # persisting or contacting a server.
        return replace(
            current,
            provider_id="openai_compatible",
            base_url="http://127.0.0.1:1234/v1",
            model="local-model",
        )
    if provider_id in {"deepseek", "mock"}:
        return validate_provider_configuration(
            replace(current, provider_id=provider_id, base_url="", model="")
        )
    raise ValueError("不支持的模型 Provider")


def load_provider_configuration() -> ProviderConfiguration:
    path = provider_config_path()
    if not path.exists():
        return ProviderConfiguration()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Provider 配置必须是 JSON 对象")
        allowed = {field for field in ProviderConfiguration.__dataclass_fields__}
        config = ProviderConfiguration(**{key: value for key, value in payload.items() if key in allowed})
        # Pre-v15 files could omit the Ollama model because the runtime used a
        # global constant. Preserve that install by migrating in memory; a
        # later explicit save writes the full v2 configuration atomically.
        if config.provider_id == "ollama" and not config.model.strip():
            config = replace(config, model=OLLAMA_MODEL)
        if config.provider_id == "ollama" and config.max_tokens < 2048:
            config = replace(config, max_tokens=2048)
        return validate_provider_configuration(config)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Provider 配置无效，已拒绝自动回退：{exc}") from exc


def save_provider_configuration(config: ProviderConfiguration) -> ProviderConfiguration:
    validated = validate_provider_configuration(config)
    path = provider_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(validated), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return validated
