from __future__ import annotations

import pytest

from app.config import Settings
from app.telegram_client import telegram_proxy


def settings(proxy_url: str | None) -> Settings:
    return Settings(
        _env_file=None,
        DB_DSN="postgresql+asyncpg://user:pass@localhost:5432/db",
        TELEGRAM_PROXY_URL=proxy_url,
    )


def test_telegram_proxy_parses_socks5_url():
    assert telegram_proxy(settings("socks5://user:p%40ss@proxy.internal:1080")) == (
        "socks5",
        "proxy.internal",
        1080,
        True,
        "user",
        "p@ss",
    )


def test_telegram_proxy_is_optional():
    assert telegram_proxy(settings(None)) is None


def test_telegram_proxy_rejects_unsupported_scheme():
    with pytest.raises(ValueError, match="Unsupported Telegram proxy scheme"):
        telegram_proxy(settings("ftp://proxy.internal:21"))
