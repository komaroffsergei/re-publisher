from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("secrets/app.env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    tg_api_id: int = Field(validation_alias="TG_API_ID")
    tg_api_hash: str = Field(validation_alias="TG_API_HASH")
    tg_phone: str | None = Field(default=None, validation_alias="TG_PHONE")
    tg_session_name: str = Field(default="/app/sessions/max_collector", validation_alias="TG_SESSION_NAME")
    folder_name: str = Field(default="MAX", validation_alias="FOLDER_NAME")
    db_dsn: str = Field(validation_alias="DB_DSN")
    collect_comments: bool = Field(default=True, validation_alias="COLLECT_COMMENTS")
    download_media: bool = Field(default=False, validation_alias="DOWNLOAD_MEDIA")
    media_dir: str = Field(default="/app/media", validation_alias="MEDIA_DIR")
    sync_limit_per_chat: int = Field(default=0, ge=0, validation_alias="SYNC_LIMIT_PER_CHAT")
    folder_refresh_seconds: int = Field(default=300, ge=10, validation_alias="FOLDER_REFRESH_SECONDS")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
