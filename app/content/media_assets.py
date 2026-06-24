from __future__ import annotations

import hashlib
import logging
import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer
from PIL import Image
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from telethon import utils

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.link_enricher import fetch_url
from app.main import safe_echo
from app.models import LinkSnapshot, MediaAsset, PostLink, TelegramPost
from app.serializers import media_type as telegram_media_type
from app.sync import maybe_download_media
from app.telegram_client import create_telegram_client

app = typer.Typer(no_args_is_help=True)
logger = logging.getLogger(__name__)


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


def telethon_entity_ref(chat_peer_id: int):
    real_id, peer_type = utils.resolve_id(chat_peer_id)
    return peer_type(real_id)


async def download_missing_telegram_media(limit: int, *, eligible_only: bool = False) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    client = create_telegram_client(settings)
    await client.connect()
    downloaded_count = 0
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized. Run: python -m app.main login")
        async with factory() as session:
            stmt = (
                select(TelegramPost)
                .where(
                    TelegramPost.media_type.is_not(None),
                    or_(TelegramPost.media_path.is_(None), TelegramPost.media_path == ""),
                    TelegramPost.is_deleted.is_(False),
                )
                .order_by(TelegramPost.updated_at.desc(), TelegramPost.id.desc())
                .limit(limit)
            )
            if eligible_only:
                from app.models import PipelineEntry

                stmt = stmt.join(PipelineEntry, PipelineEntry.source_post_id == TelegramPost.id).where(
                    PipelineEntry.is_eligible.is_(True)
                )
            posts = list((await session.execute(stmt)).scalars())
        for post in posts:
            try:
                entity = telethon_entity_ref(int(post.chat_peer_id))
                message = await client.get_messages(entity, ids=int(post.message_id))
                if not message:
                    continue
                media_path = await maybe_download_media(
                    settings,
                    message,
                    int(post.chat_peer_id),
                    int(post.message_id),
                    force=True,
                )
                if not media_path:
                    continue
                async with factory() as session:
                    await session.execute(
                        update(TelegramPost)
                        .where(TelegramPost.id == post.id)
                        .values(media_path=media_path, media_type=telegram_media_type(message), updated_at=datetime.now(timezone.utc))
                    )
                    await session.commit()
                await register_telegram_media_for_post(int(post.id))
                downloaded_count += 1
            except Exception as exc:
                logger.warning(
                    "telegram_media_backfill_failed",
                    extra={
                        "extra": {
                            "post_id": int(post.id),
                            "chat_peer_id": int(post.chat_peer_id),
                            "message_id": int(post.message_id),
                            "error": str(exc),
                        }
                    },
                )
                continue
    finally:
        await client.disconnect()
    return downloaded_count


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


@app.command("backfill-telegram")
def backfill_telegram_command(
    limit: int = limit_option(100),
    eligible_only: bool = typer.Option(False, "--eligible-only", help="Download Telegram media only for eligible pipeline entries."),
) -> None:
    """Download Telegram media for already stored posts and register media assets."""

    count = run_async(download_missing_telegram_media(limit, eligible_only=eligible_only))
    safe_echo(f"telegram_media_downloaded={count}")


if __name__ == "__main__":
    app()
