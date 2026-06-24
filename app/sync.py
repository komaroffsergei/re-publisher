from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from app.db import create_session_factory, session_scope
from app.folders import FolderChat, resolve_folder_chats
from app.models import TelegramChat, TelegramComment, TelegramPost, TelegramSyncState
from app.serializers import message_to_post_dict, peer_id
from app.telegram_client import create_telegram_client

logger = logging.getLogger(__name__)


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
            "media_path": stmt.excluded.media_path,
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
            "media_path": stmt.excluded.media_path,
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


async def upsert_post(session: AsyncSession, data: dict[str, Any]) -> int:
    return int((await session.execute(build_post_upsert(data).returning(TelegramPost.id))).scalar_one())


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


async def maybe_download_media(
    settings: Settings,
    message: Any,
    chat_peer_id: int | None,
    message_id: int,
) -> str | None:
    if not settings.download_media or getattr(message, "media", None) is None or chat_peer_id is None:
        return None
    target_dir = Path(settings.media_dir) / str(chat_peer_id) / str(message_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        downloaded = await message.download_media(file=str(target_dir))
        return str(downloaded) if downloaded else None
    except Exception as exc:
        logger.warning(
            "media_download_failed",
            extra={"extra": {"chat_peer_id": chat_peer_id, "message_id": message_id, "error": str(exc)}},
        )
        return None


async def iter_initial_messages(settings: Settings, entity: Any, client: Any, last_message_id: int | None):
    if last_message_id:
        async for message in client.iter_messages(entity, min_id=last_message_id, reverse=True):
            yield message
        return

    if settings.sync_limit_per_chat == 0:
        async for message in client.iter_messages(entity, limit=None, reverse=True):
            yield message
        return

    messages = [message async for message in client.iter_messages(entity, limit=settings.sync_limit_per_chat)]
    for message in reversed(messages):
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
) -> int:
    media_path = await maybe_download_media(settings, message, chat.peer_id, message.id)
    post_data = message_to_post_dict(client, chat.entity, message, media_path)
    post_id = await upsert_post(session, post_data)
    await ensure_pipeline_entry_for_post(session, post_id)
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
    return post_id


async def sync_chat(
    client: Any,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    chat: FolderChat,
    *,
    force_full_history: bool = False,
    collect_comments: bool = True,
    on_saved_post: Callable[[int], None] | None = None,
) -> int:
    saved = 0
    try:
        async with session_scope(session_factory) as session:
            await upsert_chat(session, settings.folder_name, chat)
            last_message_id = None if force_full_history else await get_last_message_id(session, chat.peer_id)

        if force_full_history:
            message_iter = client.iter_messages(chat.entity, limit=None, reverse=True)
        else:
            message_iter = iter_initial_messages(settings, chat.entity, client, last_message_id)
        async for message in message_iter:
            post_id = None
            async with session_scope(session_factory) as session:
                post_id = await save_message(client, settings, session, chat, message, collect_comments=collect_comments)
                saved += 1
            if on_saved_post:
                on_saved_post(post_id)
    except FloodWaitError as exc:
        await sleep_for_flood_wait(exc)
        return saved
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
    return saved


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
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized. Run: python -m app.main login")
        session_factory = create_session_factory(settings)
        chats = await resolve_and_store_chats(client, settings, session_factory, folder)
        total = 0
        for chat in chats.values():
            total += await sync_chat(client, settings, session_factory, chat)
        logger.info("sync_finished", extra={"extra": {"folder": folder, "messages_saved": total}})
    finally:
        await client.disconnect()


async def sync_folder_full_history(settings: Settings, folder_name: str | None = None, collect_comments: bool = False) -> None:
    folder = folder_name or settings.folder_name
    client = create_telegram_client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized. Run: python -m app.main login")
        session_factory = create_session_factory(settings)
        chats = await resolve_and_store_chats(client, settings, session_factory, folder)
        total = 0
        for chat in chats.values():
            total += await sync_chat(client, settings, session_factory, chat, force_full_history=True, collect_comments=collect_comments)
        logger.info(
            "full_history_sync_finished",
            extra={"extra": {"folder": folder, "messages_saved": total, "collect_comments": collect_comments}},
        )
    finally:
        await client.disconnect()


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
    table = TelegramPost.__table__
    for message_id in message_ids:
        stmt = (
            insert(table)
            .values(
                chat_peer_id=chat_peer_id,
                message_id=message_id,
                raw={},
                is_deleted=True,
            )
            .on_conflict_do_update(
                constraint="uq_telegram_posts_chat_message",
                set_={"is_deleted": True, "updated_at": func.now()},
            )
        )
        await session.execute(stmt)


async def run_service(settings: Settings, folder_name: str | None = None) -> None:
    folder = folder_name or settings.folder_name
    client = create_telegram_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram session is not authorized. Run: python -m app.main login")

    session_factory = create_session_factory(settings)
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
            total += await sync_chat(client, settings, session_factory, chat, on_saved_post=enqueue_post_processing)
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
            post_id = await save_message(client, settings, session, chat, event.message)
        enqueue_post_processing(post_id)

    @client.on(events.MessageEdited)
    async def on_message_edited(event):
        resolved_peer_id = event_peer_id(event)
        chat = current_chats.get(resolved_peer_id)
        if chat is None:
            return
        post_id = None
        async with session_scope(session_factory) as session:
            post_id = await save_message(client, settings, session, chat, event.message, collect_comments=False)
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
    queue_task = track_task(asyncio.create_task(process_queue_loop()))
    track_task(asyncio.create_task(backfill_chats(list(current_chats.values()), "startup")))
    try:
        logger.info("service_started", extra={"extra": {"folder": folder, "chat_count": len(current_chats)}})
        await client.run_until_disconnected()
    finally:
        refresh_task.cancel()
        queue_task.cancel()
        for task in list(background_tasks):
            task.cancel()
        await asyncio.gather(refresh_task, *background_tasks, return_exceptions=True)
        await client.disconnect()
