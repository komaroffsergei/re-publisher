"""Prepare an attributed copy of a Telegram post without changing its original."""

from __future__ import annotations

from app.models import TelegramChat, TelegramPost


def marked_post_text(text: str | None, source_url: str) -> str:
    body = (text or "").strip()
    attribution = f"Источник: [оригинальный пост]({source_url})"
    if attribution in body:
        return body
    return f"{body}\n\n{attribution}" if body else attribution


def telegram_post_source_url(
    post: TelegramPost | None, chat: TelegramChat | None
) -> str | None:
    if not post or not post.message_id:
        return None
    username = (
        str(chat.username or "").strip().lstrip("@") if chat and chat.username else ""
    )
    if username:
        return f"https://t.me/{username}/{post.message_id}"
    peer_id = int(post.chat_peer_id or 0)
    if peer_id < -1000000000000:
        return f"https://t.me/c/{abs(peer_id) - 1000000000000}/{post.message_id}"
    return None
