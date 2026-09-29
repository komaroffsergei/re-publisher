"""Read-only access to downloaded Telegram attachments and album members."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.models import TelegramPost


INLINE_TYPES = {
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".png": ("image/png", "image"),
    ".gif": ("image/gif", "image"),
    ".webp": ("image/webp", "image"),
    ".mp4": ("video/mp4", "video"),
    ".webm": ("video/webm", "video"),
}
DOWNLOAD_STATUS_LABELS = {
    "missing": "нет локального файла",
    "skipped_too_large": "файл больше лимита 100 МБ",
    "failed": "ошибка загрузки",
}


def local_telegram_media_path(media_dir: str, stored_path: str | None) -> Path | None:
    if not stored_path:
        return None
    root = Path(media_dir).resolve()
    path = Path(stored_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def inline_media_type(path: Path) -> tuple[str, str]:
    return INLINE_TYPES.get(path.suffix.lower(), ("application/octet-stream", "file"))


async def telegram_media_for_post(session, post: TelegramPost, media_dir: str) -> list[dict]:
    if post.grouped_id is None:
        posts = [post]
    else:
        posts = list((await session.execute(
            select(TelegramPost)
            .where(
                TelegramPost.chat_peer_id == post.chat_peer_id,
                TelegramPost.grouped_id == post.grouped_id,
                TelegramPost.is_deleted.is_(False),
            )
            .order_by(TelegramPost.message_id)
        )).scalars())
    media = []
    for member in posts:
        if member.is_deleted or not (member.media_type or member.media_path):
            continue
        path = local_telegram_media_path(media_dir, member.media_path)
        _mime, kind = inline_media_type(path) if path else ("", "file")
        media.append({
            "post_id": member.id,
            "message_id": member.message_id,
            "filename": path.name if path else None,
            "available": path is not None,
            "kind": kind,
            "download_status": member.media_download_status,
            "download_status_label": DOWNLOAD_STATUS_LABELS.get(member.media_download_status, "файл недоступен"),
        })
    return media
