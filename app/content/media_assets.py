from __future__ import annotations

import hashlib
import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.link_enricher import fetch_url
from app.main import safe_echo
from app.models import LinkSnapshot, MediaAsset, PostLink, TelegramPost

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Media asset commands."""


def extension_for(mime_type: str | None, source: str | None = None) -> str:
    guessed = mimetypes.guess_extension((mime_type or "").split(";")[0].strip())
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    if source:
        suffix = Path(source).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return suffix
    return ".bin"


def inspect_image(path: Path) -> tuple[str | None, int | None, int | None]:
    try:
        with Image.open(path) as image:
            return Image.MIME.get(image.format), image.width, image.height
    except Exception:
        return None, None, None


def asset_path(media_dir: str, sha256: str, extension: str) -> Path:
    today = datetime.now(timezone.utc)
    return Path(media_dir) / f"{today:%Y}" / f"{today:%m}" / f"{sha256}{extension}"


async def upsert_asset(session, values: dict) -> int | None:
    table = MediaAsset.__table__
    stmt = insert(table).values(**values)
    result = await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[table.c.sha256],
            index_where=table.c.sha256.is_not(None),
            set_={
                "source_type": stmt.excluded.source_type,
                "source_post_id": stmt.excluded.source_post_id,
                "source_link_id": stmt.excluded.source_link_id,
                "source_url": stmt.excluded.source_url,
                "local_path": stmt.excluded.local_path,
                "storage_url": stmt.excluded.storage_url,
                "mime_type": stmt.excluded.mime_type,
                "width": stmt.excluded.width,
                "height": stmt.excluded.height,
                "size_bytes": stmt.excluded.size_bytes,
                "download_status": stmt.excluded.download_status,
                "error": stmt.excluded.error,
                "updated_at": stmt.excluded.updated_at,
            },
        ).returning(table.c.id)
    )
    return result.scalar_one_or_none()


async def register_telegram_media(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        result = await session.execute(
            select(TelegramPost)
            .outerjoin(MediaAsset, MediaAsset.source_post_id == TelegramPost.id)
            .where(TelegramPost.media_path.is_not(None), MediaAsset.id.is_(None))
            .order_by(TelegramPost.id)
            .limit(limit)
        )
        posts = list(result.scalars())
        for post in posts:
            source = Path(post.media_path or "")
            if not source.exists():
                continue
            data = source.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            mime_type, width, height = inspect_image(source)
            target = asset_path(settings.media_dir, digest, extension_for(mime_type, source.name))
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            await upsert_asset(
                session,
                {
                    "source_type": "telegram_media",
                    "source_post_id": post.id,
                    "source_url": None,
                    "local_path": str(target),
                    "mime_type": mime_type,
                    "width": width,
                    "height": height,
                    "size_bytes": target.stat().st_size,
                    "sha256": digest,
                    "download_status": "done",
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            count += 1
        await session.commit()
    return count


async def register_telegram_media_for_post(post_id: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        post = (
            await session.execute(
                select(TelegramPost)
                .outerjoin(MediaAsset, MediaAsset.source_post_id == TelegramPost.id)
                .where(TelegramPost.id == post_id, TelegramPost.media_path.is_not(None), MediaAsset.id.is_(None))
            )
        ).scalar_one_or_none()
        if not post:
            return 0
        source = Path(post.media_path or "")
        if not source.exists():
            return 0
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        mime_type, width, height = inspect_image(source)
        target = asset_path(settings.media_dir, digest, extension_for(mime_type, source.name))
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
        await upsert_asset(
            session,
            {
                "source_type": "telegram_media",
                "source_post_id": post.id,
                "source_url": None,
                "local_path": str(target),
                "mime_type": mime_type,
                "width": width,
                "height": height,
                "size_bytes": target.stat().st_size,
                "sha256": digest,
                "download_status": "done",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
        )
        await session.commit()
        count += 1
    return count


async def download_link_images(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        result = await session.execute(
            select(LinkSnapshot)
            .where(LinkSnapshot.image_url.is_not(None), LinkSnapshot.image_asset_id.is_(None))
            .order_by(LinkSnapshot.id)
            .limit(limit)
        )
        snapshots = list(result.scalars())
        for snapshot in snapshots:
            result = await fetch_url(snapshot.image_url, settings)
            if result.error or not result.body:
                continue
            digest = hashlib.sha256(result.body).hexdigest()
            target = asset_path(settings.media_dir, digest, extension_for(result.content_type, snapshot.image_url))
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(result.body)
            mime_type, width, height = inspect_image(target)
            if not mime_type or width is None or height is None:
                continue
            asset_id = await upsert_asset(
                session,
                {
                    "source_type": "link_image",
                    "source_link_id": snapshot.link_id,
                    "source_url": snapshot.image_url,
                    "local_path": str(target),
                    "mime_type": mime_type,
                    "width": width,
                    "height": height,
                    "size_bytes": target.stat().st_size,
                    "sha256": digest,
                    "download_status": "done",
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            if asset_id:
                await session.execute(update(LinkSnapshot).where(LinkSnapshot.id == snapshot.id).values(image_asset_id=asset_id))
            count += 1
        await session.commit()
    return count


async def download_link_images_for_post(post_id: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        result = await session.execute(
            select(LinkSnapshot)
            .join(PostLink, PostLink.id == LinkSnapshot.link_id)
            .where(
                PostLink.post_id == post_id,
                LinkSnapshot.image_url.is_not(None),
                LinkSnapshot.image_asset_id.is_(None),
            )
            .order_by(LinkSnapshot.id)
        )
        snapshots = list(result.scalars())
        for snapshot in snapshots:
            result = await fetch_url(snapshot.image_url, settings)
            if result.error or not result.body:
                continue
            digest = hashlib.sha256(result.body).hexdigest()
            target = asset_path(settings.media_dir, digest, extension_for(result.content_type, snapshot.image_url))
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(result.body)
            mime_type, width, height = inspect_image(target)
            if not mime_type or width is None or height is None:
                continue
            asset_id = await upsert_asset(
                session,
                {
                    "source_type": "link_image",
                    "source_link_id": snapshot.link_id,
                    "source_url": snapshot.image_url,
                    "local_path": str(target),
                    "mime_type": mime_type,
                    "width": width,
                    "height": height,
                    "size_bytes": target.stat().st_size,
                    "sha256": digest,
                    "download_status": "done",
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            if asset_id:
                await session.execute(update(LinkSnapshot).where(LinkSnapshot.id == snapshot.id).values(image_asset_id=asset_id))
            count += 1
        await session.commit()
    return count


@app.command("download-pending")
def download_pending_command(limit: int = limit_option(100)) -> None:
    """Register local Telegram media and download pending link images."""

    count = run_async(register_telegram_media(limit)) + run_async(download_link_images(limit))
    safe_echo(f"media_assets={count}")


if __name__ == "__main__":
    app()
