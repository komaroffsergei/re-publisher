"""Отдельный отправитель MAX. Не загружает модели и не использует старые drafts.

Каждый POST предваряется записью намерения в БД. После сбоя процесса такое
намерение нельзя считать неотправленным: сначала сверяем канал. Сохранённый
MID всегда проверяем чтением; повтор POST для него запрещён.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, or_

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.models import (MaxChannel, MaxPublicationAttempt, MaxPublicationDelivery,
                        MaxPublicationPart, MaxPublicationRoute, PipelineEntry, TelegramPost)
from app.publication.max_client import MaxApiError, MaxClient
from app.publication.payload import PreparationError, receipt_sha256, verify_message, sha_json, uploaded_media_matches, source_fingerprint
from app.publication.service import (finish_delivery, now, prepare, queue_new,
                                     record_history, refresh_channel)
from app.runtime_status import heartbeat

log = logging.getLogger(__name__)
ACTIVE = ('queued', 'preparing', 'sending', 'verifying')


def read_token(path: str) -> str:
    target = Path(path)
    if os.name == 'posix' and target.stat().st_mode & 0o077:
        raise ValueError('MAX secret file must not be readable by group/others')
    return target.read_text(encoding='utf-8').strip()


def public_url(value: str | None) -> str | None:
    parsed = urlsplit(value or '')
    return value if parsed.scheme == 'https' and parsed.hostname == 'max.ru' and not parsed.username else None


async def check_requested(factory, client, *, periodic=False):
    async with factory() as session:
        condition = MaxChannel.check_requested.is_(True)
        if periodic:
            active_channels = select(MaxPublicationRoute.channel_id).where(MaxPublicationRoute.enabled.is_(True))
            condition = or_(condition, MaxChannel.id.in_(active_channels) & or_(
                MaxChannel.checked_at.is_(None), MaxChannel.checked_at < now() - timedelta(minutes=5)))
        ids = list((await session.execute(select(MaxChannel.id).where(
            condition).order_by(MaxChannel.id).limit(1))).scalars())
    for channel_id in ids:
        try:
            await refresh_channel(factory, client, channel_id)
        except (MaxApiError, PreparationError) as exc:
            async with factory() as session:
                channel = await session.get(MaxChannel, channel_id)
                channel.access_state = 'denied' if isinstance(exc, MaxApiError) and exc.status in {401, 403} else 'error'
                channel.check_requested = False
                channel.checked_at, channel.error = now(), str(exc)
                await session.commit()


async def fail(factory, delivery_id, problem, *, unknown=False, retry=False):
    async with factory() as session:
        delivery = await session.get(MaxPublicationDelivery, delivery_id, with_for_update=True)
        delivery.error = str(problem)
        delivery.next_attempt_at = None
        if unknown:
            delivery.status = 'unknown'
        elif retry and delivery.attempts < 3:
            delivery.status = 'queued'
            delivery.next_attempt_at = now() + timedelta(seconds=(10, 30, 120)[delivery.attempts])
            delivery.attempts += 1
        else:
            delivery.status = 'failed'
        await session.commit()


async def validate_snapshot(factory, delivery_id, media_dir):
    async with factory() as session:
        delivery = await session.get(MaxPublicationDelivery, delivery_id)
        route = await session.get(MaxPublicationRoute, delivery.route_id)
        snapshot = await prepare(session, delivery.entry_id, route, media_dir)
        if snapshot['payload_sha256'] != delivery.payload_sha256:
            raise PreparationError('Источник, вложения, фильтр или результат модели изменились после подготовки')


async def read_and_verify(factory, client, part_id):
    async with factory() as session:
        part = await session.get(MaxPublicationPart, part_id)
        delivery = await session.get(MaxPublicationDelivery, part.delivery_id)
        channel = await session.get(MaxChannel, delivery.channel_id)
        mid, request, receipt = part.mid, part.request, part.receipt_sha256
        chat_id = channel.chat_id
    message = await client.get_message(mid)
    # Для channel MAX может вообще не возвращать sender. Известный MID
    # получен от нашего POST, проверяем его по снимку ответа и recipient.
    sender_id = (message.get('sender') or {}).get('user_id')
    if ((sender_id is not None and sender_id != await client.identity())
            or not verify_message(message, request, chat_id=chat_id, mid=mid, receipt=receipt)):
        raise PreparationError('Опубликованное сообщение отличается: текст, канал или вложения')
    async with factory() as session:
        part = await session.get(MaxPublicationPart, part_id, with_for_update=True)
        part.status, part.verified_at, part.error = 'verified', now(), None
        part.public_url = public_url(message.get('url'))
        await session.commit()
    await record_history(factory, delivery.channel_id, [message])


async def reconcile_unknown(factory, client):
    async with factory() as session:
        row = (await session.execute(select(MaxPublicationPart, MaxPublicationDelivery, MaxChannel)
            .join(MaxPublicationDelivery, MaxPublicationDelivery.id == MaxPublicationPart.delivery_id)
            .join(MaxChannel, MaxChannel.id == MaxPublicationDelivery.channel_id)
            .where(MaxPublicationDelivery.status == 'unknown', MaxPublicationPart.mid.is_(None),
                   MaxPublicationPart.status.in_(['unknown', 'sending']),
                   or_(MaxPublicationPart.absence_checked_at.is_(None), MaxPublicationPart.absence_checked_at < now() - timedelta(seconds=30)))
            .order_by(MaxPublicationPart.id).limit(1))).first()
        if not row:
            return
        part, delivery, channel = row
        if not part.send_started_at:
            return
        lower = int((part.send_started_at - timedelta(seconds=5)).timestamp() * 1000)
        upper = int((part.send_started_at + timedelta(seconds=180)).timestamp() * 1000)
    cursor, messages, seen = upper, [], set()
    while True:
        page = await client.history(channel.chat_id, cursor)
        for message in page:
            mid = (message.get('body') or {}).get('mid')
            if mid and mid not in seen and int(message['timestamp']) >= lower:
                messages.append(message); seen.add(mid)
        if len(page) < 100 or min(int(m['timestamp']) for m in page) < lower:
            break
        next_cursor = min(int(m['timestamp']) for m in page)
        if cursor is not None and next_cursor >= cursor:
            raise PreparationError('Не удалось завершить сверку неопределённой отправки')
        cursor = next_cursor
    await record_history(factory, channel.id, messages)
    bot_id = await client.identity()
    candidates = [m for m in messages if lower <= int(m['timestamp']) <= upper
                  and (m.get('sender') or {}).get('user_id') in {None, bot_id}
                  and verify_message(m, part.request, chat_id=channel.chat_id)]
    if (len(candidates) == 1 and part.number == 1 and delivery.source_url in part.request['text']
            and (not part.request['media'] or uploaded_media_matches(part.attachments, candidates[0]))):
        # Для вложения обязателен постоянный ID. Одного совпадения числа картинок
        # недостаточно, чтобы приписать найденный пост нашей попытке.
        message = candidates[0]
        async with factory() as session:
            saved = await session.get(MaxPublicationPart, part.id, with_for_update=True)
            saved.mid = message['body']['mid']
            saved.receipt_sha256 = receipt_sha256(message)
            saved.sent_at, saved.status = now(), 'sent'
            d = await session.get(MaxPublicationDelivery, delivery.id)
            d.status, d.error = 'verifying', None
            session.add(MaxPublicationAttempt(part_id=saved.id, started_at=now(), finished_at=now(), outcome='reconciled_found'))
            await session.commit()
        await read_and_verify(factory, client, part.id)
        await finish_delivery(factory, delivery.id)
    else:
        async with factory() as session:
            saved = await session.get(MaxPublicationPart, part.id)
            saved.absence_checked_at = now()
            saved.absence_scan_sha256 = sha_json(sorted(m['body']['mid'] for m in messages))
            if not candidates and (now() - part.send_started_at).total_seconds() >= 180:
                saved.absence_scan_count += 1
                saved.error = 'Сообщение не найдено в полной сверке. Повтор требует явного подтверждения отсутствия доставки.'
            else:
                saved.absence_scan_count = 0
                saved.error = 'Найдено несколько совпадений или нельзя подтвердить вложения. Требуется ручная сверка.'
            await session.commit()


async def send_part(factory, client, part_id, media_dir):
    async with factory() as session:
        part = await session.get(MaxPublicationPart, part_id)
        delivery = await session.get(MaxPublicationDelivery, part.delivery_id)
        delivery_id, request, attachments = delivery.id, part.request, list(part.attachments or [])
        channel = await session.get(MaxChannel, delivery.channel_id)
        chat_id = channel.chat_id
        if part.mid:
            # После перезапуска GET, а не повторный POST.
            has_mid = True
        else:
            has_mid = False
            if part.status == 'sending':
                raise MaxApiError(None, 'interrupted_send', unknown=True)
    if has_mid:
        await read_and_verify(factory, client, part_id)
        return
    await validate_snapshot(factory, delivery_id, media_dir)
    for media in request['media'][len(attachments):]:
        attachments.append(await client.upload(media))
        async with factory() as session:
            part = await session.get(MaxPublicationPart, part_id)
            part.attachments = list(attachments)
            part.status = 'uploaded'
            await session.commit()
    # Источник мог отредактироваться, пока передавался большой файл.
    await validate_snapshot(factory, delivery_id, media_dir)
    async with factory() as session:
        part = await session.get(MaxPublicationPart, part_id)
        delivery = await session.get(MaxPublicationDelivery, delivery_id)
        channel = await session.get(MaxChannel, delivery.channel_id)
        previous = await session.scalar(select(MaxPublicationPart).where(
            MaxPublicationPart.delivery_id == delivery_id,
            MaxPublicationPart.number == part.number - 1))
        reply_mid = previous.mid if previous else None
        if previous and previous.status != 'verified':
            raise PreparationError('Предыдущая часть ещё не подтверждена')
        delay = max(0, 1 - (now() - channel.last_sent_at).total_seconds()) if channel.last_sent_at else 0
    if delay:
        await asyncio.sleep(delay)
    async with factory() as session:
        part = await session.get(MaxPublicationPart, part_id, with_for_update=True)
        if part.mid or part.status in {'sending', 'unknown', 'sent', 'verified'}:
            # Повторный процесс не может перезаписать уже начатую попытку.
            raise MaxApiError(None, 'concurrent_or_interrupted_send', unknown=True)
        part.status, part.send_started_at = 'sending', now()
        delivery = await session.get(MaxPublicationDelivery, delivery_id)
        delivery.status = 'sending'
        channel = await session.get(MaxChannel, delivery.channel_id)
        channel.last_sent_at = part.send_started_at
        attempt = MaxPublicationAttempt(part_id=part_id, started_at=part.send_started_at, outcome='started',
                                        request_sha256=sha_json(part.request))
        session.add(attempt)
        await session.commit()
        attempt_id = attempt.id
    try:
        message = await client.send(chat_id, request['text'], attachments, reply_mid)
    except MaxApiError as exc:
        async with factory() as session:
            attempt = await session.get(MaxPublicationAttempt, attempt_id)
            attempt.finished_at, attempt.outcome = now(), 'unknown' if exc.unknown else 'not_sent'
            attempt.http_status, attempt.error_code = exc.status, exc.code
            part = await session.get(MaxPublicationPart, part_id)
            part.status = 'unknown' if exc.unknown else 'uploaded'
            await session.commit()
        raise
    # Этот commit должен предшествовать любой последующей сетевой проверке.
    async with factory() as session:
        part = await session.get(MaxPublicationPart, part_id)
        part.mid = message['body']['mid']
        part.receipt_sha256 = receipt_sha256(message)
        part.public_url = public_url(message.get('url'))
        part.sent_at, part.status = now(), 'sent'
        attempt = await session.get(MaxPublicationAttempt, attempt_id)
        attempt.finished_at, attempt.outcome = now(), 'sent'
        delivery = await session.get(MaxPublicationDelivery, delivery_id)
        delivery.status = 'verifying'
        await session.commit()
    await read_and_verify(factory, client, part_id)


async def process_one(factory, client, media_dir):
    async with factory() as session:
        delivery = await session.scalar(select(MaxPublicationDelivery).where(
            MaxPublicationDelivery.status.in_(ACTIVE),
            or_(MaxPublicationDelivery.next_attempt_at.is_(None), MaxPublicationDelivery.next_attempt_at <= now()))
            .order_by(MaxPublicationDelivery.id).limit(1))
        if not delivery:
            return False
        delivery_id = delivery.id
        parts = list((await session.execute(select(MaxPublicationPart).where(
            MaxPublicationPart.delivery_id == delivery_id).order_by(MaxPublicationPart.number))).scalars())
        pending = next((p.id for p in parts if p.status not in {'verified', 'cancelled'}), None)
    if pending is None:
        await finish_delivery(factory, delivery_id)
        return True
    try:
        await send_part(factory, client, pending, media_dir)
        await finish_delivery(factory, delivery_id)
    except MaxApiError as exc:
        await fail(factory, delivery_id, exc, unknown=exc.unknown,
                   retry=exc.status in {None, 429, 500, 502, 503, 504} or exc.code == 'attachment.not.ready')
    except PreparationError as exc:
        await fail(factory, delivery_id, exc)
    return True


async def watch_source_changes(factory, cursor=0):
    from app.taxonomy.jobs import text_sha256
    async with factory() as session:
        rows = (await session.execute(select(MaxPublicationDelivery, TelegramPost)
            .join(PipelineEntry, PipelineEntry.id == MaxPublicationDelivery.entry_id)
            .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
            .where(MaxPublicationDelivery.status == 'delivered', MaxPublicationDelivery.source_changed_at.is_(None),
                   MaxPublicationDelivery.id > cursor).order_by(MaxPublicationDelivery.id).limit(100))).all()
        for delivery, post in rows:
            posts = list((await session.execute(select(TelegramPost).where(
                TelegramPost.chat_peer_id == post.chat_peer_id, TelegramPost.grouped_id == post.grouped_id)
                .order_by(TelegramPost.message_id))).scalars()) if post.grouped_id is not None else [post]
            if (post.is_deleted or text_sha256(post.text) != delivery.text_sha256
                    or source_fingerprint(posts) != delivery.snapshot.get('source_fingerprint')):
                delivery.source_changed_at = now()
        await session.commit()
        return rows[-1][0].id if rows else 0


async def runtime_heartbeat(factory):
    while True:
        await heartbeat(factory, 'publication-worker')
        await asyncio.sleep(10)


async def main():
    settings = get_settings().model_copy(update={'db_pool_size': 1, 'db_max_overflow': 0})
    engine = create_engine(settings)
    factory = create_session_factory(engine=engine)
    client = MaxClient(read_token(settings.max_bot_token_file))
    pulse = asyncio.create_task(runtime_heartbeat(factory))
    try:
        tick, changed_cursor = 0, 0
        while True:
            try:
                await check_requested(factory, client, periodic=settings.max_publisher_enabled)
                if settings.max_publisher_enabled:
                    await queue_new(factory, settings.media_dir)
                    await process_one(factory, client, settings.media_dir)
                    await reconcile_unknown(factory, client)
                if tick % 30 == 0:
                    changed_cursor = await watch_source_changes(factory, changed_cursor)
                await heartbeat(factory, 'publication-worker', success=True)
                tick += 1
            except Exception as exc:
                # repr исключения HTTP/драйвера может содержать секрет или текст.
                log.error('Publication loop failed: %s', type(exc).__name__)
                await heartbeat(factory, 'publication-worker', error=type(exc).__name__)
            await asyncio.sleep(1)
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError):
            await pulse
        await client.aclose()
        await engine.dispose()


if __name__ == '__main__':
    import fcntl
    with open('/tmp/publisher-max-sender.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(main())
