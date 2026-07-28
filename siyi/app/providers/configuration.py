from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.config import settings


OLLAMA_BASE_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen3:4b"
PROVIDER_IDS = {"deepseek", "ollama", "mock"}


@dataclass(frozen=True)
class ProviderConfiguration:
    provider_id: str = "deepseek"
    base_url: str = ""
    model: str = ""
    timeout_seconds: int = 90
    max_tokens: int = 8192
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
    if config.provider_id == "ollama":
        parsed = urlsplit(config.base_url or OLLAMA_BASE_URL)
        if parsed.scheme != "http" or (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama 只允许本机 HTTP 回环地址")
        if parsed.port != 11434 or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Ollama 只允许本机 11434 端口")
        if (config.model or OLLAMA_MODEL) != OLLAMA_MODEL:
            raise ValueError(f"本地测试模型固定为 {OLLAMA_MODEL}")
    return config


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
