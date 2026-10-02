"""Только локальные файлы и первый кадр. Превью не является оригиналом."""
from __future__ import annotations
import asyncio
from pathlib import Path
from uuid import uuid4


def safe_path(value, root):
    if not value:
        return None
    path, directory = Path(value).resolve(), Path(root).resolve()
    return path if path.is_relative_to(directory) and path.is_file() else None


def static_thumbnail(message):
    # VideoSize нельзя передавать OCR как картинку.
    from telethon.tl.types import PhotoCachedSize, PhotoSize, PhotoSizeProgressive
    thumbs = getattr(getattr(message, "document", None), "thumbs", None) or []
    candidates = [item for item in thumbs if isinstance(item, (PhotoSize, PhotoCachedSize, PhotoSizeProgressive))]
    return max(candidates, key=lambda item: item.w * item.h, default=None)


async def download_preview(message, settings, peer, message_id):
    thumbnail = static_thumbnail(message)
    if thumbnail is None:
        return None, "no_static_preview"
    directory = Path(settings.ocr_preview_dir) / str(peer)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{message_id}-{uuid4().hex}.part"
    final = directory / f"{message_id}.jpg"
    try:
        def limit(received, total):
            if received > 10 * 1024 * 1024:
                raise ValueError("Preview exceeds size budget")
        result = await message.download_media(file=str(temporary), thumb=thumbnail, progress_callback=limit)
        if not result:
            return None, "missing"
        Path(result).replace(final)
        return str(final), "downloaded"
    finally:
        temporary.unlink(missing_ok=True)


async def first_frame(video: Path, directory: Path, timeout=30) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"frame-{uuid4().hex}.png"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
        "-protocol_whitelist", "file,pipe", "-i", str(video),
        "-frames:v", "1", "-vf", "scale=1600:1600:force_original_aspect_ratio=decrease",
        "-y", str(target), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(), timeout)
        if process.returncode or not target.is_file():
            raise ValueError("First video frame is unavailable")
        return target
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        target.unlink(missing_ok=True)
        raise
