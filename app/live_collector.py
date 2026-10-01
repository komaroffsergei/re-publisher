"""MAX: новые сообщения с постоянной границей; старая обработка не вызывается."""

from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select, update
from telethon import events
from telethon.errors import FloodWaitError
from app.db import create_engine, create_session_factory
from app.folders import resolve_folder_chats
from app.models import ServiceRuntime, TelegramSyncState, TelegramPost, PipelineEntry
from app.runtime_status import heartbeat
from app.telegram_client import create_telegram_client
from app.sync import (
    upsert_chat,
    save_message,
    event_peer_id,
    mark_messages_deleted,
    is_within_sync_window,
    sleep_for_flood_wait,
)

logger = logging.getLogger(__name__)


def eligible(message, boundary):
    # Сообщение без даты не позволяет доказать, что оно новое.
    return getattr(message, "date", None) is not None and is_within_sync_window(
        message, boundary
    )


async def register_chats(factory, chats):
    now = datetime.now(timezone.utc)
    async with factory() as session:
        async with session.begin():
            service = await session.get(ServiceRuntime, "collector")
            first = service is None
            if first:
                service = ServiceRuntime(name="collector", started_at=now)
                session.add(service)
            for chat in chats.values():
                await upsert_chat(session, "MAX", chat)
                state = await session.get(TelegramSyncState, chat.peer_id)
                if state is None:
                    state = TelegramSyncState(chat_peer_id=chat.peer_id)
                    session.add(state)
                if state.live_since is None or not state.live_member:
                    state.live_since = service.started_at if first else now
                    state.reconciled_message_id = None
                state.live_member = True
            await session.execute(
                update(TelegramSyncState)
                .where(
                    TelegramSyncState.live_member.is_(True),
                    TelegramSyncState.chat_peer_id.not_in(list(chats)),
                )
                .values(live_member=False)
            )


async def reconcile_chat(client, factory, chat, persist):
    # Курсор сверки обновляется только после полного закрытого диапазона.
    # NewMessage может сохранить более высокий ID и не влияет на этот курсор.
    while True:
        try:
            async with factory() as session:
                state = await session.get(TelegramSyncState, chat.peer_id)
                cursor, boundary = state.reconciled_message_id, state.live_since
            tail = await client.get_messages(chat.entity, limit=1)
            upper = tail[0].id if tail else (cursor or 0)
            if upper > (cursor or 0):
                kwargs = dict(limit=None, min_id=cursor or 0, max_id=upper + 1)
                if cursor is not None:
                    kwargs["reverse"] = True
                async for message in client.iter_messages(chat.entity, **kwargs):
                    if not eligible(message, boundary):
                        if cursor is None:
                            break
                        continue
                    await persist(chat, message)
            async with factory() as session:
                state = await session.get(TelegramSyncState, chat.peer_id)
                state.reconciled_message_id = upper
                state.last_synced_at = datetime.now(timezone.utc)
                state.error = None
                await session.commit()
            return True
        except FloodWaitError as exc:
            await sleep_for_flood_wait(exc)
        except Exception as exc:
            async with factory() as session:
                state = await session.get(TelegramSyncState, chat.peer_id)
                state.error = type(exc).__name__
                await session.commit()
            logger.exception(
                "live_chat_reconcile_failed", extra={"extra": {"peer_id": chat.peer_id}}
            )
            return False


async def run_live(settings):
    if (
        settings.collector_process_saved_posts
        or settings.collect_comments
        or settings.auto_publish
    ):
        raise RuntimeError(
            "new_only requires old processing, comments and publishing to be disabled"
        )
    client = create_telegram_client(settings)
    engine = create_engine(settings)
    factory = create_session_factory(engine=engine)
    downloads = asyncio.Semaphore(2)
    chats = {}
    tasks = []
    # Realtime и сверка одного чата не записывают один пост одновременно.
    locks = {}

    async def persist(chat, message, editing=False):
        async with locks.setdefault(chat.peer_id, asyncio.Lock()):
            return await persist_locked(chat, message, editing)

    async def persist_locked(chat, message, editing=False):
        async with factory() as session:
            state = await session.get(TelegramSyncState, chat.peer_id)
            existing = (
                await session.execute(
                    select(TelegramPost).where(
                        TelegramPost.chat_peer_id == chat.peer_id,
                        TelegramPost.message_id == message.id,
                    )
                )
            ).scalar_one_or_none()
            if not state or not state.live_member:
                return
            if not eligible(message, state.live_since) and not (editing and existing):
                return
            if (
                existing
                and existing.text == (getattr(message, "message", None) or None)
                and existing.edit_date == getattr(message, "edit_date", None)
                and existing.media_download_status not in {"failed", "pending"}
            ):
                return
        for attempt in range(4):
            async with downloads:
                async with factory() as session:
                    async with session.begin():
                        post_id, inserted, media_status = await save_message(
                            client,
                            settings,
                            session,
                            chat,
                            message,
                            collect_comments=False,
                        )
                        entry = (
                            await session.execute(
                                select(PipelineEntry)
                                .where(PipelineEntry.source_post_id == post_id)
                                .with_for_update()
                            )
                        ).scalar_one()
                        # Новый участник альбома либо обновлённое вложение отзывает
                        # готовность известных карточек группы до повторной проверки.
                        related = (
                            select(PipelineEntry)
                            .join(
                                TelegramPost,
                                TelegramPost.id == PipelineEntry.source_post_id,
                            )
                            .where(
                                TelegramPost.chat_peer_id == chat.peer_id,
                                PipelineEntry.auto_enabled.is_(True),
                                (
                                    (PipelineEntry.stage == "ready")
                                    | (
                                        (PipelineEntry.auto_phase == "media")
                                        & (
                                            PipelineEntry.auto_state.in_(
                                                ("blocked", "stopped")
                                            )
                                        )
                                        & (media_status == "downloaded")
                                    )
                                ),
                            )
                        )
                        grouped = getattr(message, "grouped_id", None)
                        related = (
                            related.where(TelegramPost.grouped_id == grouped)
                            if grouped
                            else related.where(TelegramPost.id == post_id)
                        )
                        for prepared in (
                            await session.execute(
                                related.with_for_update(of=PipelineEntry)
                            )
                        ).scalars():
                            prepared.stage = "marking"
                            prepared.ready_at = None
                            prepared.auto_state = "pending"
                            prepared.auto_retry_at = None
                        if inserted:
                            entry.auto_enabled = True
                            entry.auto_state = "pending"
            if media_status != "failed" or attempt == 3:
                return
            await asyncio.sleep((10, 30, 120)[attempt])

    async def refresh():
        nonlocal chats
        while True:
            try:
                resolved = {
                    c.peer_id: c for c in await resolve_folder_chats(client, "MAX")
                }
                await register_chats(factory, resolved)
                chats = resolved
                successful = True
                for chat in list(chats.values()):
                    successful = (
                        await reconcile_chat(client, factory, chat, persist)
                        and successful
                    )
                # Ручной повтор загрузки использует существующее сообщение, не историю.
                async with factory() as session:
                    pending = (
                        await session.execute(
                            select(TelegramPost.chat_peer_id, TelegramPost.message_id)
                            .join(
                                PipelineEntry,
                                PipelineEntry.source_post_id == TelegramPost.id,
                            )
                            .where(
                                PipelineEntry.auto_enabled.is_(True),
                                TelegramPost.media_download_status == "pending",
                            )
                            .limit(20)
                        )
                    ).all()
                for peer, message_id in pending:
                    message = None
                    if peer in chats:
                        message = await client.get_messages(
                            chats[peer].entity, ids=message_id
                        )
                        if message:
                            await persist(chats[peer], message, editing=True)
                    if not message:
                        async with factory() as session:
                            await session.execute(
                                update(TelegramPost)
                                .where(
                                    TelegramPost.chat_peer_id == peer,
                                    TelegramPost.message_id == message_id,
                                )
                                .values(
                                    media_download_status="failed",
                                    media_error="Сообщение или чат недоступны",
                                )
                            )
                            await session.commit()
                if successful:
                    await heartbeat(factory, "collector", success=True)
                else:
                    await heartbeat(factory, "collector", error="Ошибка сверки чата")
            except FloodWaitError as exc:
                await sleep_for_flood_wait(exc)
            except Exception as exc:
                await heartbeat(factory, "collector", error=type(exc).__name__)
                logger.exception("live_folder_refresh_failed")
            await asyncio.sleep(settings.folder_refresh_seconds)

    async def pulse():
        while True:
            if client.is_connected() and tasks and not tasks[0].done():
                await heartbeat(factory, "collector")
            await asyncio.sleep(15)

    async def on_message(event, editing=False):
        chat = chats.get(event_peer_id(event))
        if chat:
            try:
                await persist(chat, event.message, editing)
            except FloodWaitError as exc:
                await sleep_for_flood_wait(exc)
                await persist(chat, event.message, editing)
            except Exception:
                logger.exception("live_event_failed")

    async def on_deleted(event):
        peer = event_peer_id(event)
        if peer in chats:
            async with factory() as session:
                async with session.begin():
                    await mark_messages_deleted(session, peer, list(event.deleted_ids))

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized")
        chats = {c.peer_id: c for c in await resolve_folder_chats(client, "MAX")}
        await register_chats(factory, chats)
        client.add_event_handler(on_message, events.NewMessage)

        async def edited(event):
            await on_message(event, True)

        client.add_event_handler(edited, events.MessageEdited)
        client.add_event_handler(on_deleted, events.MessageDeleted)
        tasks = [asyncio.create_task(refresh()), asyncio.create_task(pulse())]
        await client.run_until_disconnected()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.disconnect()
        await engine.dispose()
