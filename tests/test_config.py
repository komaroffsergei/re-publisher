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
    assert settings.download_media is False
