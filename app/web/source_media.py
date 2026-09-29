"""Read already-downloaded Telegram media without running the processing pipeline."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Iterable

from app.models import TelegramPost


INLINE_IMAGES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}
INLINE_VIDEOS = {"video/mp4", "video/webm", "video/ogg"}


def album_primary(posts: list[TelegramPost]) -> TelegramPost:
    """Prefer the album member carrying its caption, then the earliest message."""
    if not posts:
        raise ValueError("album has no messages")
    ordered = sorted(posts, key=lambda post: post.message_id)
    return next((post for post in ordered if (post.text or "").strip()), ordered[0])


def downloaded_media_path(post: TelegramPost, media_dir: str) -> Path | None:
    """Never serve a path outside the configured media directory."""
    if post.media_download_status != "downloaded" or not post.media_path:
        return None
    root = Path(media_dir).resolve()
    path = Path(post.media_path).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


def source_media_items(posts: Iterable[TelegramPost], media_dir: str) -> list[dict]:
    items = []
    for post in sorted(posts, key=lambda post: post.message_id):
        path = downloaded_media_path(post, media_dir)
        if not post.media_type and path is None:
            continue
        mime = (mimetypes.guess_type(path.name)[0] if path else None) or "application/octet-stream"
        kind = "image" if mime in INLINE_IMAGES else "video" if mime in INLINE_VIDEOS else "file"
        items.append({
            "post_id": post.id,
            "message_id": post.message_id,
            "filename": path.name if path else None,
            "url": f"/source-media/{post.id}" if path else None,
            "mime_type": mime,
            "kind": kind,
            "size_bytes": post.media_size_bytes,
            "status": post.media_download_status,
        })
    return items
