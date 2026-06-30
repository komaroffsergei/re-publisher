from __future__ import annotations

from app.config import Settings


def test_config_loads_from_environment(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("DB_DSN", "postgresql+asyncpg://user:pass@localhost:5432/db")

    settings = Settings(_env_file=None)

    assert settings.tg_api_id == 12345
    assert settings.tg_api_hash == "hash"
    assert settings.folder_name == "MAX"
    assert settings.collect_comments is True
    assert settings.download_media is True


def test_config_loads_yandex_settings(monkeypatch):
    monkeypatch.setenv("DB_DSN", "postgresql+asyncpg://user:pass@localhost:5432/db")
    monkeypatch.setenv("ENABLE_EXTERNAL_LLM", "true")
    monkeypatch.setenv("SUMMARY_BACKEND", "yandexgpt")
    monkeypatch.setenv("REWRITE_BACKEND", "yandexgpt")
    monkeypatch.setenv("YANDEX_API_KEY", "secret-value")
    monkeypatch.setenv("YANDEX_API_KEY_ID", "key-id")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "folder-id")

    settings = Settings(_env_file=None)

    assert settings.enable_external_llm is True
    assert settings.summary_backend == "yandexgpt"
    assert settings.rewrite_backend == "yandexgpt"
    assert settings.yandex_api_key == "secret-value"
    assert settings.yandex_api_key_id == "key-id"
    assert settings.yandex_folder_id == "folder-id"
