import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_path: Path = Path("data/agent.db")
    deployment_mode: Literal["desktop_local", "local_web", "web_control", "cloud_executor"] = "desktop_local"
    bind_host: str = "127.0.0.1"
    api_token: str = ""
    deepseek_api_key: str = ""
    tavily_api_key: str = ""
    brave_api_key: str = ""
    default_search_provider: Literal["tavily", "brave"] = "tavily"
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    model_temperature: float = Field(default=0.2, ge=0, le=2)
    model_max_tokens: int = Field(default=8192, ge=1, le=1_000_000)
    model_timeout_seconds: int = Field(default=90, ge=1, le=600)
    model_connect_timeout_seconds: int = Field(default=15, ge=1, le=120)
    model_max_retries: int = Field(default=2, ge=0, le=10)
    model_routing_enabled: bool = True
    model_escalation_enabled: bool = True
    model_data_routing_enabled: bool = True
    model_light_name: str = "deepseek-v4-flash"
    model_medium_name: str = "deepseek-v4-flash"
    model_strong_name: str = "deepseek-v4-pro"
    model_light_max_tokens: int = Field(default=2048, ge=1, le=1_000_000)
    model_medium_max_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    model_strong_max_tokens: int = Field(default=8192, ge=1, le=1_000_000)
    model_low_confidence_threshold: float = Field(default=0.55, ge=0, le=1)
    model_min_observation_samples: int = Field(default=5, ge=1, le=1000)
    model_min_success_rate: float = Field(default=0.65, ge=0, le=1)
    model_pricing_json: str = "{}"
    model_context_profiles_json: str = "{}"
    default_model_context_window: int = Field(default=65_536, ge=8_192, le=10_000_000)
    context_compaction_threshold: float = Field(default=0.8, ge=0.5, le=0.95)
    context_provider_overhead_tokens: int = Field(default=2_048, ge=0, le=100_000)
    context_safety_margin_tokens: int = Field(default=8_192, ge=512, le=1_000_000)
    max_agent_rounds: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=48, ge=1, le=1000)
    max_task_tokens: int = Field(default=120_000, ge=1, le=10_000_000)
    max_phase_tokens: int = Field(default=60_000, ge=1, le=10_000_000)
    max_model_call_tokens: int = Field(default=32_000, ge=1, le=1_000_000)
    max_tool_result_chars: int = Field(default=12_000, ge=500, le=1_000_000)
    max_file_snippet_chars: int = Field(default=8_000, ge=500, le=1_000_000)
    max_skill_context_chars: int = Field(default=24_000, ge=1000, le=1_000_000)
    max_skill_count: int = Field(default=3, ge=0, le=20)
    max_memory_context_chars: int = Field(default=6_000, ge=500, le=100_000)
    max_memory_items: int = Field(default=6, ge=0, le=50)
    read_cache_ttl_seconds: int = Field(default=120, ge=0, le=3600)
    max_parallel_tool_calls: int = Field(default=4, ge=1, le=16)
    max_consecutive_failures: int = Field(default=3, ge=1, le=20)
    max_duplicate_tool_calls: int = Field(default=3, ge=2, le=20)
    max_no_progress_rounds: int = Field(default=3, ge=1, le=20)
    max_repair_attempts: int = Field(default=2, ge=0, le=5)
    task_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    max_concurrent_tasks: int = Field(default=2, ge=1, le=32)
    task_queue_timeout_seconds: int = Field(default=30, ge=1, le=600)
    multi_agent_enabled: bool = False
    multi_agent_max_children: int = Field(default=3, ge=1, le=8)
    multi_agent_max_concurrency: int = Field(default=3, ge=1, le=8)
    multi_agent_total_token_budget: int = Field(default=24_000, ge=1_000, le=1_000_000)
    multi_agent_child_token_budget: int = Field(default=8_000, ge=500, le=250_000)
    multi_agent_child_timeout_seconds: int = Field(default=45, ge=5, le=600)
    multi_agent_child_rounds: int = Field(default=3, ge=1, le=10)
    multi_agent_file_lock_seconds: int = Field(default=180, ge=10, le=3600)
    extension_directory: Path | None = None
    extension_max_files: int = Field(default=200, ge=1, le=5000)
    extension_max_bytes: int = Field(default=10_000_000, ge=100_000, le=500_000_000)
    log_path: Path = Path("data/logs/agent.log")
    log_max_bytes: int = Field(default=5_000_000, ge=100_000, le=100_000_000)
    log_backup_count: int = Field(default=5, ge=1, le=50)
    allow_local_mcp: bool = False
    allow_private_model_provider: bool = False
    network_allow_http: bool = False
    network_allowed_domains: str = ""
    network_blocked_domains: str = "metadata.google.internal,metadata.azure.internal,metadata.aws.internal"
    network_max_response_bytes: int = Field(default=5_000_000, ge=1024, le=100_000_000)
    network_max_redirects: int = Field(default=3, ge=0, le=10)
    security_snapshot_max_files: int = Field(default=20_000, ge=100, le=1_000_000)
    security_snapshot_max_bytes: int = Field(default=250_000_000, ge=1_000_000, le=10_000_000_000)
    security_snapshot_retention: int = Field(default=10, ge=1, le=100)
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://tauri.localhost,https://tauri.localhost,tauri://localhost"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_", extra="ignore")

    @property
    def origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def network_allowed_domain_list(self) -> tuple[str, ...]:
        return tuple(item.strip().lower() for item in self.network_allowed_domains.split(",") if item.strip())

    @property
    def network_blocked_domain_list(self) -> tuple[str, ...]:
        return tuple(item.strip().lower() for item in self.network_blocked_domains.split(",") if item.strip())

    @property
    def model_routes(self) -> dict[str, str]:
        return {
            "light": self.model_light_name.strip() or self.model_name,
            "medium": self.model_medium_name.strip() or self.model_name,
            "strong": self.model_strong_name.strip() or self.model_name,
        }

    @property
    def model_pricing(self) -> dict[str, dict[str, float]]:
        try:
            payload: Any = json.loads(self.model_pricing_json or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        normalized: dict[str, dict[str, float]] = {}
        for model, prices in payload.items():
            if not isinstance(model, str) or not isinstance(prices, dict):
                continue
            try:
                normalized[model] = {
                    "input": max(0.0, float(prices.get("input") or 0)),
                    "output": max(0.0, float(prices.get("output") or 0)),
                }
            except (TypeError, ValueError):
                continue
        return normalized

    @property
    def model_context_profiles(self) -> dict[str, dict[str, Any]]:
        try:
            payload: Any = json.loads(self.model_context_profiles_json or "{}")
        except (TypeError, ValueError):
            return {}
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)} if isinstance(payload, dict) else {}


settings = Settings()
