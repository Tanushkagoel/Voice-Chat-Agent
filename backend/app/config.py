from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./data/alder.db"
    redis_url: str | None = None
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_tts_model: str = "gemini-2.5-flash-preview-tts"
    gemini_tts_voice: str = "Kore"
    livekit_url: str | None = None
    livekit_public_url: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: str | None = None
    api_base_url: str = "http://127.0.0.1:8000"
    record_webhook_url: str | None = None
    upload_dir: str = "./data/uploads"
    static_dir: str | None = None
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    alder_today: str | None = None
    max_upload_bytes: int = 8 * 1024 * 1024
    gemini_input_usd_per_million: float = 0.0
    gemini_output_usd_per_million: float = 0.0

    @field_validator("database_url", mode="before")
    @classmethod
    def _postgres_async_driver(cls, value: str) -> str:
        # Render supplies postgres://. SQLAlchemy needs the asyncpg driver named.
        if value.startswith("postgres://"):
            return "postgresql+asyncpg://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value[len("postgresql://") :]
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def upload_path(self) -> Path:
        return Path(self.upload_dir)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
