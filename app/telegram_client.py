from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from telethon import TelegramClient

from app.config import Settings


def telegram_proxy(settings: Settings):
    if not settings.telegram_proxy_url:
        return None
    parsed = urlparse(settings.telegram_proxy_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"socks5", "socks4", "http"}:
        raise ValueError(f"Unsupported Telegram proxy scheme: {scheme}")
    if not parsed.hostname or not parsed.port:
        raise ValueError("TELEGRAM_PROXY_URL must contain a host and port")
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    return scheme, parsed.hostname, parsed.port, True, username, password


def create_telegram_client(settings: Settings) -> TelegramClient:
    settings.require_telegram()
    session_path = Path(settings.tg_session_name)
    if session_path.parent != Path("."):
        session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        settings.tg_session_name,
        settings.tg_api_id,
        settings.tg_api_hash,
        proxy=telegram_proxy(settings),
    )


def secure_session_permissions(settings: Settings) -> None:
    """Keep the reusable MTProto credential readable only by the service user."""

    configured = str(settings.tg_session_name)
    session_path = Path(configured if configured.endswith(".session") else f"{configured}.session")
    if not session_path.exists():
        return
    session_path.chmod(0o600)
    if session_path.parent != Path("."):
        session_path.parent.chmod(0o700)
