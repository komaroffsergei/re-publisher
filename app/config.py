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

    tg_api_id: int | None = Field(default=None, validation_alias="TG_API_ID")
    tg_api_hash: str | None = Field(default=None, validation_alias="TG_API_HASH")
    tg_phone: str | None = Field(default=None, validation_alias="TG_PHONE")
    tg_session_name: str = Field(default="/app/sessions/max_collector", validation_alias="TG_SESSION_NAME")
    folder_name: str = Field(default="MAX", validation_alias="FOLDER_NAME")
    db_dsn: str = Field(validation_alias="DB_DSN")
    collect_comments: bool = Field(default=True, validation_alias="COLLECT_COMMENTS")
    collector_process_saved_posts: bool = Field(default=True, validation_alias="COLLECTOR_PROCESS_SAVED_POSTS")
    download_media: bool = Field(default=True, validation_alias="DOWNLOAD_MEDIA")
    media_dir: str = Field(default="./media", validation_alias="MEDIA_DIR")
    sync_limit_per_chat: int = Field(default=0, ge=0, validation_alias="SYNC_LIMIT_PER_CHAT")
    folder_refresh_seconds: int = Field(default=300, ge=10, validation_alias="FOLDER_REFRESH_SECONDS")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    cache_dir: str = Field(default="./cache", validation_alias="CACHE_DIR")
    artifacts_dir: str = Field(default="./artifacts", validation_alias="ARTIFACTS_DIR")
    reports_dir: str = Field(default="./reports", validation_alias="REPORTS_DIR")
    classifier_active_model_path: str = Field(
        default="./models/active/tfidf_logreg.joblib",
        validation_alias="CLASSIFIER_ACTIVE_MODEL_PATH",
    )
    classifier_min_confidence: float = Field(default=0.70, validation_alias="CLASSIFIER_MIN_CONFIDENCE")
    classifier_high_confidence: float = Field(default=0.90, validation_alias="CLASSIFIER_HIGH_CONFIDENCE")
    allow_pseudo_labels: bool = Field(default=False, validation_alias="ALLOW_PSEUDO_LABELS")
    auto_accept_codex_labels: bool = Field(default=False, validation_alias="AUTO_ACCEPT_CODEX_LABELS")

    summary_backend: str = Field(default="extractive_fallback", validation_alias="SUMMARY_BACKEND")
    summary_model_name: str = Field(default="cointegrated/rut5-base-absum", validation_alias="SUMMARY_MODEL_NAME")
    summary_alt_model_name: str = Field(default="IlyaGusev/rut5_base_sum_gazeta", validation_alias="SUMMARY_ALT_MODEL_NAME")
    summary_device: str = Field(default="cpu", validation_alias="SUMMARY_DEVICE")
    summary_max_input_tokens: int = Field(default=900, ge=128, validation_alias="SUMMARY_MAX_INPUT_TOKENS")
    summary_max_output_tokens: int = Field(default=180, ge=32, validation_alias="SUMMARY_MAX_OUTPUT_TOKENS")
    enable_local_summary: bool = Field(default=True, validation_alias="ENABLE_LOCAL_SUMMARY")

    translation_backend: str = Field(default="argos", validation_alias="TRANSLATION_BACKEND")
    translation_default_target_lang: str = Field(default="ru", validation_alias="TRANSLATION_DEFAULT_TARGET_LANG")
    enable_translation: bool = Field(default=True, validation_alias="ENABLE_TRANSLATION")
    enable_rewrite: bool = Field(default=True, validation_alias="ENABLE_REWRITE")
    rewrite_backend: str = Field(default="local_template", validation_alias="REWRITE_BACKEND")
    enable_external_llm: bool = Field(default=False, validation_alias="ENABLE_EXTERNAL_LLM")
    enable_codex_nightly: bool = Field(default=False, validation_alias="ENABLE_CODEX_NIGHTLY")

    yandex_api_key: str | None = Field(default=None, validation_alias="YANDEX_API_KEY")
    yandex_api_key_id: str | None = Field(default=None, validation_alias="YANDEX_API_KEY_ID")
    yandex_folder_id: str | None = Field(default=None, validation_alias="YANDEX_FOLDER_ID")
    yandex_summary_model_uri: str | None = Field(default=None, validation_alias="YANDEX_SUMMARY_MODEL_URI")
    yandex_rewrite_model_uri: str | None = Field(default=None, validation_alias="YANDEX_REWRITE_MODEL_URI")
    yandex_api_url: str = Field(
        default="https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
        validation_alias="YANDEX_API_URL",
    )
    yandex_timeout_seconds: float = Field(default=30.0, gt=0, validation_alias="YANDEX_TIMEOUT_SECONDS")
    yandex_max_retries: int = Field(default=2, ge=0, validation_alias="YANDEX_MAX_RETRIES")

    local_llm_api_base: str = Field(default="http://127.0.0.1:8091/v1", validation_alias="LOCAL_LLM_API_BASE")
    local_llm_model: str = Field(default="IlyaGusev/saiga_nemo_12b_gguf:Q4_K_M", validation_alias="LOCAL_LLM_MODEL")
    local_llm_timeout_seconds: float = Field(default=300.0, gt=0, validation_alias="LOCAL_LLM_TIMEOUT_SECONDS")
    local_llm_max_tokens: int = Field(default=900, ge=64, validation_alias="LOCAL_LLM_MAX_TOKENS")
    local_llm_temperature: float = Field(default=0.35, ge=0, le=2, validation_alias="LOCAL_LLM_TEMPERATURE")

    auto_approve_drafts: bool = Field(default=False, validation_alias="AUTO_APPROVE_DRAFTS")
    auto_publish: bool = Field(default=False, validation_alias="AUTO_PUBLISH")
    bot_token: str | None = Field(default=None, validation_alias="BOT_TOKEN")
    max_bot_token: str | None = Field(default=None, validation_alias="MAX_BOT_TOKEN")
    max_channel_chat_id: str | None = Field(default=None, validation_alias="MAX_CHANNEL_CHAT_ID")
    max_channel_link: str | None = Field(default=None, validation_alias="MAX_CHANNEL_LINK")
    max_api_base: str = Field(default="https://platform-api2.max.ru", validation_alias="MAX_API_BASE")
    max_publish_random_min_minutes: int = Field(default=1, ge=0, validation_alias="MAX_PUBLISH_RANDOM_MIN_MINUTES")
    max_publish_random_max_minutes: int = Field(default=20, ge=1, validation_alias="MAX_PUBLISH_RANDOM_MAX_MINUTES")
    max_publish_loop_interval_seconds: int = Field(default=30, ge=5, validation_alias="MAX_PUBLISH_LOOP_INTERVAL_SECONDS")
    pipeline_showcase_slug: str = Field(default="ai_education", validation_alias="PIPELINE_SHOWCASE_SLUG")
    pipeline_ready_backlog_limit: int = Field(default=50, ge=1, validation_alias="PIPELINE_READY_BACKLOG_LIMIT")
    pipeline_rewrite_limit: int = Field(default=10, ge=1, validation_alias="PIPELINE_REWRITE_LIMIT")
    pipeline_use_latest_candidate_model: bool = Field(default=True, validation_alias="PIPELINE_USE_LATEST_CANDIDATE_MODEL")
    pipeline_default_publish_delay_hours: int = Field(default=24, ge=0, validation_alias="PIPELINE_DEFAULT_PUBLISH_DELAY_HOURS")

    link_fetch_timeout_seconds: float = Field(default=12.0, gt=0, validation_alias="LINK_FETCH_TIMEOUT_SECONDS")
    link_fetch_max_bytes: int = Field(default=8_000_000, gt=0, validation_alias="LINK_FETCH_MAX_BYTES")
    link_fetch_max_redirects: int = Field(default=3, ge=0, validation_alias="LINK_FETCH_MAX_REDIRECTS")
    link_fetch_user_agent: str = Field(default="TelegramContentBot/1.0", validation_alias="LINK_FETCH_USER_AGENT")

    nightly_min_new_posts_for_training: int = Field(default=20, ge=0, validation_alias="NIGHTLY_MIN_NEW_POSTS_FOR_TRAINING")
    model_promotion_min_macro_f1_delta: float = Field(default=-0.01, validation_alias="MODEL_PROMOTION_MIN_MACRO_F1_DELTA")
    model_promotion_min_weighted_f1_delta: float = Field(default=-0.01, validation_alias="MODEL_PROMOTION_MIN_WEIGHTED_F1_DELTA")
    model_promotion_min_class_f1: float = Field(default=0.25, ge=0, le=1, validation_alias="MODEL_PROMOTION_MIN_CLASS_F1")

    web_secret_key: str = Field(default="local-dev-change-me", validation_alias="WEB_SECRET_KEY")
    web_basic_auth_user: str | None = Field(default=None, validation_alias="WEB_BASIC_AUTH_USER")
    web_basic_auth_password: str | None = Field(default=None, validation_alias="WEB_BASIC_AUTH_PASSWORD")

    def require_telegram(self) -> None:
        if self.tg_api_id is None or not self.tg_api_hash:
            raise RuntimeError("TG_API_ID and TG_API_HASH are required for Telegram commands.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
