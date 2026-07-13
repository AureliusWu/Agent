from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_path: Path = Path("data/agent.db")
    deepseek_api_key: str = ""
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-chat"
    model_temperature: float = Field(default=0.2, ge=0, le=2)
    model_max_tokens: int = Field(default=8192, ge=1, le=1_000_000)
    model_timeout_seconds: int = Field(default=90, ge=1, le=600)
    model_connect_timeout_seconds: int = Field(default=15, ge=1, le=120)
    model_max_retries: int = Field(default=2, ge=0, le=10)
    max_agent_rounds: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=48, ge=1, le=1000)
    max_task_tokens: int = Field(default=120_000, ge=1, le=10_000_000)
    max_consecutive_failures: int = Field(default=3, ge=1, le=20)
    max_duplicate_tool_calls: int = Field(default=3, ge=2, le=20)
    max_no_progress_rounds: int = Field(default=3, ge=1, le=20)
    max_repair_attempts: int = Field(default=2, ge=0, le=5)
    task_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    max_concurrent_tasks: int = Field(default=2, ge=1, le=32)
    task_queue_timeout_seconds: int = Field(default=30, ge=1, le=600)
    log_path: Path = Path("data/logs/agent.log")
    log_max_bytes: int = Field(default=5_000_000, ge=100_000, le=100_000_000)
    log_backup_count: int = Field(default=5, ge=1, le=50)
    allow_local_mcp: bool = False
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://tauri.localhost,https://tauri.localhost,tauri://localhost"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_", extra="ignore")

    @property
    def origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
