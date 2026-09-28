from __future__ import annotations

import asyncio
import logging
import random
import shutil
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import events, utils
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    UserNotParticipantError,
)

from app.comments import collect_comments_for_post
from app.config import Settings
from app.content.pipeline_activity import reset_stale_pipeline_activity, try_acquire_pipeline_work_lock
from app.content.pipeline_entries import ensure_pipeline_entry_for_post, sync_pipeline_entry_stage
from app.db import create_engine, create_session_factory, session_scope
from app.folders import FolderChat, resolve_folder_chats
from app.models import TelegramChat, TelegramComment, TelegramPost, TelegramSyncState
from app.serializers import message_to_post_dict, peer_id
from app.telegram_client import create_telegram_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaDownloadResult:
    path: str | None
    status: str
    size_bytes: int | None = None
    error: str | None = None


@dataclass
class ChatSyncMetrics:
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_old: int = 0
    media_downloaded: int = 0
    media_skipped: int = 0
    media_failed: int = 0

    @property
    def saved(self) -> int:
        return self.inserted + self.updated

    def add_media(self, status: str) -> None:
        if status == "downloaded":
            self.media_downloaded += 1
        elif status == "skipped_too_large":
            self.media_skipped += 1
        elif status == "failed":
            self.media_failed += 1

    def merge(self, other: ChatSyncMetrics) -> None:
        self.seen += other.seen
        self.inserted += other.inserted
        self.updated += other.updated
        self.skipped_old += other.skipped_old
        self.media_downloaded += other.media_downloaded
        self.media_skipped += other.media_skipped
        self.media_failed += other.media_failed

    def as_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped_old": self.skipped_old,
            "media_downloaded": self.media_downloaded,
            "media_skipped": self.media_skipped,
            "media_failed": self.media_failed,
        }


def build_chat_upsert(data: dict[str, Any]):
    table = TelegramChat.__table__
    stmt = insert(table).values(**data)
    return stmt.on_conflict_do_update(
        index_elements=[table.c.peer_id],
        set_={
            "title": stmt.excluded.title,
            "username": stmt.excluded.username,
            "chat_type": stmt.excluded.chat_type,
            "folder_name": stmt.excluded.folder_name,
            "raw": stmt.excluded.raw,
            "updated_at": func.now(),
        },
    )


def build_post_upsert(data: dict[str, Any]):
    table = TelegramPost.__table__
    stmt = insert(table).values(**data)
    return stmt.on_conflict_do_update(
        constraint="uq_telegram_posts_chat_message",
        set_={
            "sender_peer_id": stmt.excluded.sender_peer_id,
            "date": stmt.excluded.date,
            "edit_date": stmt.excluded.edit_date,
            "text": stmt.excluded.text,
            "grouped_id": stmt.excluded.grouped_id,
            "views": stmt.excluded.views,
            "forwards": stmt.excluded.forwards,
            "replies_count": stmt.excluded.replies_count,
            "media_type": stmt.excluded.media_type,
            "media_path": func.coalesce(stmt.excluded.media_path, table.c.media_path),
            "media_size_bytes": func.coalesce(stmt.excluded.media_size_bytes, table.c.media_size_bytes),
            "media_download_status": stmt.excluded.media_download_status,
            "media_error": stmt.excluded.media_error,
            "raw": stmt.excluded.raw,
            "is_deleted": stmt.excluded.is_deleted,
            "updated_at": func.now(),
        },
    )


def build_comment_upsert(data: dict[str, Any]):
    table = TelegramComment.__table__
    stmt = insert(table).values(**data)
    return stmt.on_conflict_do_update(
        constraint="uq_telegram_comments_discussion_message",
        set_={
            "post_chat_peer_id": stmt.excluded.post_chat_peer_id,
            "post_message_id": stmt.excluded.post_message_id,
            "parent_comment_message_id": stmt.excluded.parent_comment_message_id,
            "sender_peer_id": stmt.excluded.sender_peer_id,
            "date": stmt.excluded.date,
            "edit_date": stmt.excluded.edit_date,
            "text": stmt.excluded.text,
            "media_type": stmt.excluded.media_type,
            "media_path": func.coalesce(stmt.excluded.media_path, table.c.media_path),
            "raw": stmt.excluded.raw,
            "is_deleted": stmt.excluded.is_deleted,
            "updated_at": func.now(),
        },
    )


def build_sync_state_upsert(chat_peer_id: int, last_message_id: int | None = None, error: str | None = None):
    table = TelegramSyncState.__table__
    values = {
        "chat_peer_id": chat_peer_id,
        "last_message_id": last_message_id,
        "last_synced_at": datetime.now(timezone.utc) if error is None else None,
        "error": error,
        "updated_at": datetime.now(timezone.utc),
    }
    stmt = insert(table).values(**values)
    excluded_last = stmt.excluded.last_message_id
    merged_last_message_id = case(
        (excluded_last.is_(None), table.c.last_message_id),
        else_=func.greatest(func.coalesce(table.c.last_message_id, excluded_last), excluded_last),
    )
    return stmt.on_conflict_do_update(
        index_elements=[table.c.chat_peer_id],
        set_={
            "last_message_id": merged_last_message_id,
            "last_synced_at": func.coalesce(stmt.excluded.last_synced_at, table.c.last_synced_at),
            "error": stmt.excluded.error,
            "updated_at": func.now(),
        },
    )


async def upsert_chat(session: AsyncSession, folder_name: str, chat: FolderChat) -> None:
    await session.execute(
        build_chat_upsert(
            {
                "peer_id": chat.peer_id,
                "title": chat.title,
                "username": chat.username,
                "chat_type": chat.chat_type,
                "folder_name": folder_name,
                "raw": chat.raw,
            }
        )
    )


async def upsert_post(session: AsyncSession, data: dict[str, Any]) -> tuple[int, bool]:
    existing_id = (
        await session.execute(
            select(TelegramPost.id).where(
                TelegramPost.chat_peer_id == data["chat_peer_id"],
                TelegramPost.message_id == data["message_id"],
            )
        )
    ).scalar_one_or_none()
    post_id = int((await session.execute(build_post_upsert(data).returning(TelegramPost.id))).scalar_one())
    return post_id, existing_id is None


async def upsert_comment(session: AsyncSession, data: dict[str, Any]) -> None:
    await session.execute(build_comment_upsert(data))


async def update_sync_state(
    session: AsyncSession,
    chat_peer_id: int,
    last_message_id: int | None = None,
    error: str | None = None,
) -> None:
    await session.execute(build_sync_state_upsert(chat_peer_id, last_message_id, error))


async def get_last_message_id(session: AsyncSession, chat_peer_id: int) -> int | None:
    result = await session.execute(
        select(TelegramSyncState.last_message_id).where(TelegramSyncState.chat_peer_id == chat_peer_id)
    )
    return result.scalar_one_or_none()


def message_media_size(message: Any) -> int | None:
    value = getattr(getattr(message, "file", None), "size", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def download_message_media(
    settings: Settings,
    message: Any,
    chat_peer_id: int | None,
    message_id: int,
    *,
    force: bool = False,
) -> MediaDownloadResult:
    if getattr(message, "media", None) is None or chat_peer_id is None:
        return MediaDownloadResult(None, "missing")
    size_bytes = message_media_size(message)
    if not force and not settings.download_media:
        return MediaDownloadResult(None, "disabled", size_bytes)
    if size_bytes is not None and size_bytes > settings.media_max_bytes:
        return MediaDownloadResult(None, "skipped_too_large", size_bytes, "media exceeds configured limit")

    target_dir = Path(settings.media_dir) / str(chat_peer_id) / str(message_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    existing = next((path for path in target_dir.iterdir() if path.is_file() and not path.name.endswith(".part")), None)
    if existing is not None:
        return MediaDownloadResult(str(existing), "downloaded", existing.stat().st_size)

    staging_dir = target_dir / f".tmp-{uuid4().hex}"
    staging_dir.mkdir()

    def check_progress(received: int, _total: int) -> None:
        if received > settings.media_max_bytes:
            raise ValueError("media exceeds configured limit")

    try:
        downloaded = await message.download_media(file=str(staging_dir), progress_callback=check_progress)
        if not downloaded:
            return MediaDownloadResult(None, "missing", size_bytes)
        downloaded_path = Path(downloaded)
        actual_size = downloaded_path.stat().st_size
        if actual_size > settings.media_max_bytes:
            return MediaDownloadResult(None, "skipped_too_large", actual_size, "media exceeds configured limit")
        final_path = target_dir / downloaded_path.name
        downloaded_path.replace(final_path)
        return MediaDownloadResult(str(final_path), "downloaded", actual_size)
    except FloodWaitError:
        raise
    except ValueError as exc:
        return MediaDownloadResult(None, "skipped_too_large", size_bytes, str(exc))
    except Exception as exc:
        logger.warning(
            "media_download_failed",
            extra={"extra": {"chat_peer_id": chat_peer_id, "message_id": message_id, "error": str(exc)}},
        )
        return MediaDownloadResult(None, "failed", size_bytes, str(exc)[:800])
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


async def maybe_download_media(
    settings: Settings,
    message: Any,
    chat_peer_id: int | None,
    message_id: int,
    *,
    force: bool = False,
) -> str | None:
    return (await download_message_media(settings, message, chat_peer_id, message_id, force=force)).path


def sync_cutoff(settings: Settings, now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) - timedelta(hours=settings.sync_lookback_hours)


def is_within_sync_window(message: Any, cutoff: datetime) -> bool:
    message_date = getattr(message, "date", None)
    if message_date is None:
        return True
    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=timezone.utc)
    return message_date.astimezone(timezone.utc) >= cutoff


async def iter_initial_messages(
    settings: Settings,
    entity: Any,
    client: Any,
    last_message_id: int | None,
    *,
    now: datetime | None = None,
    metrics: ChatSyncMetrics | None = None,
):
    cutoff = sync_cutoff(settings, now)
    kwargs = {"min_id": last_message_id} if last_message_id else {}
    async for message in client.iter_messages(entity, **kwargs):
        if not is_within_sync_window(message, cutoff):
            if metrics is not None:
                metrics.skipped_old += 1
            break
        yield message


async def sleep_for_flood_wait(exc: FloodWaitError) -> None:
    seconds = int(getattr(exc, "seconds", 0)) + random.uniform(1, 3)
    logger.warning("flood_wait_sleep", extra={"extra": {"seconds": seconds}})
    await asyncio.sleep(seconds)


async def save_message(
    client: Any,
    settings: Settings,
    session: AsyncSession,
    chat: FolderChat,
    message: Any,
    collect_comments: bool = True,
    update_state: bool = True,
) -> tuple[int, bool, str]:
    media = await download_message_media(settings, message, chat.peer_id, message.id)
    post_data = message_to_post_dict(
        client,
        chat.entity,
        message,
        media.path,
        media_size_bytes=media.size_bytes,
        media_download_status=media.status,
        media_error=media.error,
    )
    post_id, inserted = await upsert_post(session, post_data)
    await ensure_pipeline_entry_for_post(session, post_id)
    if update_state:
        await update_sync_state(session, chat.peer_id, message.id)
    if collect_comments:
        await collect_comments_for_post(
            client,
            settings,
            session,
            post_data,
            chat.entity,
            message,
            maybe_download_media,
            upsert_comment,
        )
    return post_id, inserted, media.status


async def sync_chat(
    client: Any,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    chat: FolderChat,
    *,
    force_full_history: bool = False,
    collect_comments: bool = True,
    on_saved_post: Callable[[int], None] | None = None,
) -> ChatSyncMetrics:
    metrics = ChatSyncMetrics()
    try:
        while True:
            try:
                async with session_scope(session_factory) as session:
                    await upsert_chat(session, settings.folder_name, chat)
                    last_message_id = None if force_full_history else await get_last_message_id(session, chat.peer_id)

                max_message_id = last_message_id
                if force_full_history:
                    message_iter = client.iter_messages(chat.entity, limit=None, reverse=True)
                else:
                    message_iter = iter_initial_messages(
                        settings,
                        chat.entity,
                        client,
                        last_message_id,
                        metrics=metrics,
                    )
                async for message in message_iter:
                    metrics.seen += 1
                    async with session_scope(session_factory) as session:
                        post_id, inserted, media_status = await save_message(
                            client,
                            settings,
                            session,
                            chat,
                            message,
                            collect_comments=collect_comments,
                            update_state=False,
                        )
                    if inserted:
                        metrics.inserted += 1
                    else:
                        metrics.updated += 1
                    metrics.add_media(media_status)
                    max_message_id = max(int(getattr(message, "id")), max_message_id or 0)
                    if on_saved_post:
                        on_saved_post(post_id)
                async with session_scope(session_factory) as session:
                    await update_sync_state(session, chat.peer_id, max_message_id)
                break
            except FloodWaitError as exc:
                await sleep_for_flood_wait(exc)
                continue
    except (ChannelPrivateError, ChatAdminRequiredError, UserNotParticipantError) as exc:
        async with session_scope(session_factory) as session:
            await update_sync_state(session, chat.peer_id, error=exc.__class__.__name__)
        logger.warning(
            "chat_skipped",
            extra={"extra": {"peer_id": chat.peer_id, "title": chat.title, "error": exc.__class__.__name__}},
        )
    except Exception as exc:
        async with session_scope(session_factory) as session:
            await update_sync_state(session, chat.peer_id, error=str(exc))
        logger.exception(
            "chat_sync_failed",
            extra={"extra": {"peer_id": chat.peer_id, "title": chat.title, "error": str(exc)}},
        )
    logger.info("chat_sync_finished", extra={"extra": {"peer_id": chat.peer_id, **metrics.as_dict()}})
    return metrics


async def resolve_and_store_chats(
    client: Any,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    folder_name: str,
) -> dict[int, FolderChat]:
    chats = await resolve_folder_chats(client, folder_name)
    by_peer_id = {chat.peer_id: chat for chat in chats}
    async with session_scope(session_factory) as session:
        for chat in chats:
            await upsert_chat(session, folder_name, chat)
    logger.info("folder_resolved", extra={"extra": {"folder": folder_name, "chat_count": len(chats)}})
    return by_peer_id


async def sync_folder(settings: Settings, folder_name: str | None = None) -> None:
    folder = folder_name or settings.folder_name
    client = create_telegram_client(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine=engine)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized. Run: python -m app.main login")
        chats = await resolve_and_store_chats(client, settings, session_factory, folder)
        total = ChatSyncMetrics()
        for chat in chats.values():
            result = await sync_chat(
                client,
                settings,
                session_factory,
                chat,
                collect_comments=settings.collect_comments,
            )
            total.merge(result)
        logger.info("sync_finished", extra={"extra": {"folder": folder, **total.as_dict()}})
    finally:
        await client.disconnect()
        await engine.dispose()


async def sync_folder_full_history(settings: Settings, folder_name: str | None = None, collect_comments: bool = False) -> None:
    folder = folder_name or settings.folder_name
    client = create_telegram_client(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine=engine)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized. Run: python -m app.main login")
        chats = await resolve_and_store_chats(client, settings, session_factory, folder)
        total = 0
        for chat in chats.values():
            total += (
                await sync_chat(
                    client,
                    settings,
                    session_factory,
                    chat,
                    force_full_history=True,
                    collect_comments=collect_comments,
                )
            ).saved
        logger.info(
            "full_history_sync_finished",
            extra={"extra": {"folder": folder, "messages_saved": total, "collect_comments": collect_comments}},
        )
    finally:
        await client.disconnect()
        await engine.dispose()


def event_peer_id(event: Any) -> int | None:
    event_peer = getattr(event, "peer_id", None)
    resolved = peer_id(event_peer)
    if resolved is not None:
        return resolved
    chat_id = getattr(event, "chat_id", None)
    if chat_id is not None:
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return None
    return None


async def mark_messages_deleted(session: AsyncSession, chat_peer_id: int, message_ids: list[int]) -> None:
    if not message_ids:
        return
    await session.execute(
        TelegramPost.__table__.update()
        .where(
            TelegramPost.chat_peer_id == chat_peer_id,
            TelegramPost.message_id.in_(message_ids),
        )
        .values(is_deleted=True, updated_at=func.now())
    )


async def run_service(settings: Settings, folder_name: str | None = None) -> None:
    folder = folder_name or settings.folder_name
    client = create_telegram_client(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine=engine)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        await engine.dispose()
        raise RuntimeError("Telegram session is not authorized. Run: python -m app.main login")

    async with session_scope(session_factory) as session:
        await reset_stale_pipeline_activity(session)
    current_chats = await resolve_and_store_chats(client, settings, session_factory, folder)
    background_tasks: set[asyncio.Task] = set()
    processing_queue: asyncio.Queue[int] = asyncio.Queue()
    queued_posts: set[int] = set()

    def track_task(task: asyncio.Task) -> asyncio.Task:
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    def enqueue_post_processing(post_id: int | None) -> None:
        if not settings.collector_process_saved_posts:
            return
        if post_id is None or post_id in queued_posts:
            return
        queued_posts.add(post_id)
        processing_queue.put_nowait(post_id)

    async def process_queue_loop() -> None:
        from app.content.link_enricher import enrich_post_links
        from app.content.local_summary import summarize_post_links
        from app.content.material_builder import build_post_material
        from app.content.media_assets import download_link_images_for_post, register_telegram_media_for_post
        from app.content.pipeline_manager import classify_post
        from app.content.processor import process_post
        from app.content.state import mark_state
        from app.content.url_extractor import extract_post_links

        while True:
            post_id = await processing_queue.get()
            queued_posts.discard(post_id)
            current_status_field: str | None = None

            async def set_processing_status(field_name: str, value: str, error: str | None = None) -> None:
                error_value = "" if error is None and value in {"running", "processing"} else error
                async with session_scope(session_factory) as session:
                    await mark_state(session, post_id, **{field_name: value}, last_error=error_value)

            async def run_queue_stage(field_name: str, label: str, coro) -> Any:
                nonlocal current_status_field
                current_status_field = field_name
                await set_processing_status(field_name, "running", None)
                logger.info("post_processing_stage_started", extra={"extra": {"post_id": post_id, "stage": label}})
                result = await coro
                await set_processing_status(field_name, "done", None)
                logger.info("post_processing_stage_finished", extra={"extra": {"post_id": post_id, "stage": label}})
                return result

            lock = None
            try:
                while lock is None:
                    lock = await try_acquire_pipeline_work_lock(settings, owner="collector", entry_id=post_id)
                    if lock is None:
                        await asyncio.sleep(2)
                await run_queue_stage("processing_status", "process_post", process_post(post_id))
                await run_queue_stage("link_status", "extract_links", extract_post_links(post_id))
                await run_queue_stage("enrichment_status", "enrich_links", enrich_post_links(post_id))
                await run_queue_stage("enrichment_status", "telegram_media", register_telegram_media_for_post(post_id))
                await run_queue_stage("enrichment_status", "link_images", download_link_images_for_post(post_id))
                await run_queue_stage("summary_status", "summarize_links", summarize_post_links(post_id))
                await run_queue_stage("material_status", "build_material", build_post_material(post_id, refresh=True))
                await run_queue_stage("classification_status", "classify_post", classify_post(post_id))
                async with session_scope(session_factory) as session:
                    await sync_pipeline_entry_stage(session, post_id)
            except Exception as exc:
                if current_status_field:
                    await set_processing_status(current_status_field, "failed", str(exc)[:800])
                logger.exception("post_processing_failed", extra={"extra": {"post_id": post_id, "error": str(exc)}})
            finally:
                if lock is not None:
                    await lock.release()
                processing_queue.task_done()

    async def backfill_chats(chats: list[FolderChat], reason: str) -> None:
        total = 0
        logger.info("backfill_started", extra={"extra": {"folder": folder, "chat_count": len(chats), "reason": reason}})
        for chat in chats:
            total += (
                await sync_chat(
                    client,
                    settings,
                    session_factory,
                    chat,
                    collect_comments=settings.collect_comments,
                    on_saved_post=enqueue_post_processing,
                )
            ).saved
        logger.info(
            "backfill_finished",
            extra={"extra": {"folder": folder, "chat_count": len(chats), "messages_saved": total, "reason": reason}},
        )

    async def refresh_loop() -> None:
        nonlocal current_chats
        while True:
            await asyncio.sleep(settings.folder_refresh_seconds)
            try:
                previous_peer_ids = set(current_chats)
                refreshed_chats = await resolve_and_store_chats(client, settings, session_factory, folder)
                current_chats = refreshed_chats
                added = [
                    chat
                    for peer_id, chat in refreshed_chats.items()
                    if peer_id not in previous_peer_ids
                ]
                removed_count = len(previous_peer_ids - set(refreshed_chats))
                if added or removed_count:
                    logger.info(
                        "folder_membership_changed",
                        extra={
                            "extra": {
                                "folder": folder,
                                "added_count": len(added),
                                "removed_count": removed_count,
                                "chat_count": len(refreshed_chats),
                            }
                        },
                    )
                if added:
                    track_task(asyncio.create_task(backfill_chats(added, "folder_refresh")))
            except FloodWaitError as exc:
                await sleep_for_flood_wait(exc)
            except Exception as exc:
                logger.warning("folder_refresh_failed", extra={"extra": {"folder": folder, "error": str(exc)}})

    @client.on(events.NewMessage)
    async def on_new_message(event):
        resolved_peer_id = event_peer_id(event)
        chat = current_chats.get(resolved_peer_id)
        if chat is None:
            return
        post_id = None
        async with session_scope(session_factory) as session:
            post_id, _inserted, _media_status = await save_message(
                client,
                settings,
                session,
                chat,
                event.message,
                collect_comments=settings.collect_comments,
            )
        enqueue_post_processing(post_id)

    @client.on(events.MessageEdited)
    async def on_message_edited(event):
        resolved_peer_id = event_peer_id(event)
        chat = current_chats.get(resolved_peer_id)
        if chat is None:
            return
        post_id = None
        async with session_scope(session_factory) as session:
            existing = (
                await session.execute(
                    select(TelegramPost.id).where(
                        TelegramPost.chat_peer_id == resolved_peer_id,
                        TelegramPost.message_id == event.message.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None and not is_within_sync_window(event.message, sync_cutoff(settings)):
                return
            post_id, _inserted, _media_status = await save_message(
                client,
                settings,
                session,
                chat,
                event.message,
                collect_comments=False,
            )
        enqueue_post_processing(post_id)

    @client.on(events.MessageDeleted)
    async def on_message_deleted(event):
        resolved_peer_id = event_peer_id(event)
        if resolved_peer_id is None or resolved_peer_id not in current_chats:
            logger.warning("deleted_message_chat_unresolved")
            return
        async with session_scope(session_factory) as session:
            await mark_messages_deleted(session, resolved_peer_id, list(event.deleted_ids))

    refresh_task = asyncio.create_task(refresh_loop())
    queue_task = track_task(asyncio.create_task(process_queue_loop())) if settings.collector_process_saved_posts else None
    track_task(asyncio.create_task(backfill_chats(list(current_chats.values()), "startup")))
    try:
        logger.info("service_started", extra={"extra": {"folder": folder, "chat_count": len(current_chats)}})
        await client.run_until_disconnected()
    finally:
        refresh_task.cancel()
        if queue_task is not None:
            queue_task.cancel()
        for task in list(background_tasks):
            task.cancel()
        await asyncio.gather(refresh_task, *background_tasks, return_exceptions=True)
        await client.disconnect()
        await engine.dispose()
