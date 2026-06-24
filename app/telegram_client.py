from __future__ import annotations

from pathlib import Path

from telethon import TelegramClient

from app.config import Settings


def create_telegram_client(settings: Settings) -> TelegramClient:
    settings.require_telegram()
    session_path = Path(settings.tg_session_name)
    if session_path.parent != Path("."):
        session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(settings.tg_session_name, settings.tg_api_id, settings.tg_api_hash)
