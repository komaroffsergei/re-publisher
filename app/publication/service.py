"""Подготовка outbox: текущий фильтр и текст проверяются перед каждой отправкой."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from sqlalchemy import select, or_
from sqlalchemy.dialects.postgresql import insert

from app.content.post_preparation import album_posts, readiness_error
from app.content.selection_filters import current_assessment
from app.taxonomy.profiles import profile_of
from app.models import (FilterMark, MaxChannel, MaxObservedMessage, MaxPublicationBatch,
                        MaxPublicationControl, MaxPublicationDelivery, MaxPublicationPart,
                        MaxPublicationRoute, PipelineEntry, PostFilterMark, SelectionFilter,
                        SelectionFilterVersion, TaxonomyClassification, TelegramChat, TelegramPost)
from app.publication.payload import PreparationError, build_snapshot, message_parts, sha_json
from app.publication.review_guard import PublicationHold, hold_reason, policy_error
from app.taxonomy.jobs import text_sha256
from app.web.source_media import album_primary


def now():
    return datetime.now(timezone.utc)


def gate_error(route, version) -> str | None:
    gate = route.quality_gate or {}
    matched, correct = gate.get('test_matched', 0), gate.get('test_correct', 0)
    if (route.approved_version_id != version.id or gate.get('expression_sha256') != sha_json(version.expression)
            or gate.get('model_key') != version.model_key):
        return 'Изменён фильтр: нужна новая проверка модели'
    train_positive = gate.get('train_positive', 0)
    if (type(matched) is not int or type(correct) is not int or matched < 50
            or not 0 <= correct <= matched or correct / matched < 0.9
            or type(train_positive) is not int or train_positive < 1000
            or not isinstance(gate.get('model_version'), str) or not gate['model_version'].strip()
            or any(not isinstance(gate.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', gate[key]) for key in ['test_sha256', 'split_sha256'])):
        return 'Не пройдена проверка корпуса и отложенных совпадений'
    return policy_error(gate.get('review_policy'))


async def route_version(session, route):
    pair = (await session.execute(select(SelectionFilter, SelectionFilterVersion)
        .join(SelectionFilterVersion, SelectionFilterVersion.id == SelectionFilter.active_version_id)
        .where(SelectionFilter.id == route.filter_id))).first()
    mark = await session.get(FilterMark, route.mark_id)
    if not pair or not pair[0].enabled or pair[0].archived or not mark or mark.archived or pair[1].mark_id != route.mark_id:
        raise PreparationError('Маршрут не связан с действующим фильтром и лейблом')
    if problem := gate_error(route, pair[1]):
        raise PreparationError(problem)
    return pair[1]


async def prepare(session, entry_id: int, route, media_dir: str) -> dict:
    version = await route_version(session, route)
    channel = await session.get(MaxChannel, route.channel_id)
    if not route.enabled or not channel or channel.access_state != 'ok':
        raise PreparationError('Маршрут выключен или у бота нет подтверждённых прав')
    if channel.history_checked_at is None:
        raise PreparationError('История канала ещё не сверена на повторы')
    row = (await session.execute(select(PipelineEntry, TelegramPost, TelegramChat)
        .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
        .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
        .where(PipelineEntry.id == entry_id))).first()
    if not row or row[0].stage != 'ready':
        raise PreparationError('Пост ещё не на стадии «На публикацию»')
    entry, post, chat = row
    if problem := await readiness_error(session, entry, post, media_dir):
        raise PreparationError(problem)
    if (await session.scalar(select(PostFilterMark.id).where(
            PostFilterMark.entry_id == entry.id, PostFilterMark.mark_id == route.mark_id,
            PostFilterMark.active.is_(True)))) is None:
        raise PreparationError('Пользовательский лейбл снят')
    job = await session.scalar(select(TaxonomyClassification).where(
        TaxonomyClassification.pipeline_entry_id == entry.id,
        TaxonomyClassification.model_key == version.model_key,
        TaxonomyClassification.profile == profile_of(version)))
    if (not job or job.model_version != route.quality_gate['model_version']
            or (await current_assessment(session, version, job, post))['outcome'] != 'matched'):
        raise PreparationError('Нет актуального совпадения проверенной версии модели')
    items = list((await session.execute(select(TelegramPost).where(
        TelegramPost.chat_peer_id == post.chat_peer_id, TelegramPost.grouped_id == post.grouped_id)
        .order_by(TelegramPost.message_id))).scalars()) if post.grouped_id is not None else [post]
    if any(item.is_deleted for item in items):
        raise PreparationError('В исходном альбоме удалено сообщение: нужна ручная проверка состава')
    if album_primary(items).id != post.id:
        raise PreparationError('Для альбома используется только основная карточка с подписью')
    snapshot = await asyncio.to_thread(build_snapshot, items, chat, entry.marked_source_url, media_dir)
    snapshot.update(text_sha256=text_sha256(post.text), route_version_id=version.id,
                    model_version=job.model_version, run_id=job.current_run_id,
                    review_policy_sha256=route.quality_gate['review_policy']['sha256'])
    snapshot['payload_sha256'] = sha_json(snapshot)
    if reason := hold_reason(snapshot, route.quality_gate['review_policy']):
        raise PublicationHold(reason, snapshot)
    return snapshot


async def record_hold(session, entry_id, route, issue):
    """Нет частей отправки: held никогда не подхватывается MAX worker.

    Уникальные ограничения сохраняют первый разбор, повторный опрос не
    размножает записи. Уже существующая доставка не перезаписывается.
    """
    snapshot = issue.snapshot
    await session.execute(insert(MaxPublicationDelivery).values(
        entry_id=entry_id, route_id=route.id, channel_id=route.channel_id,
        source_key=snapshot['source_key'], source_url=snapshot['source_url'],
        text_sha256=snapshot['text_sha256'], content_sha256=snapshot['content_sha256'],
        payload_sha256=snapshot['payload_sha256'], snapshot=snapshot,
        status='held', error=str(issue)).on_conflict_do_nothing())


async def enqueue_delivery(session, entry_id: int, route, media_dir: str, batch_id=None, frozen_sha=None):
    snapshot = await prepare(session, entry_id, route, media_dir)
    if frozen_sha and snapshot['payload_sha256'] != frozen_sha:
        raise PreparationError('Пост изменился после фиксации партии')
    if await already_observed(session, route.channel_id, snapshot, include_deliveries=False):
        raise PreparationError('Источник уже найден в истории целевого канала')
    statement = insert(MaxPublicationDelivery).values(
        entry_id=entry_id, route_id=route.id, channel_id=route.channel_id, batch_id=batch_id,
        source_key=snapshot['source_key'], source_url=snapshot['source_url'],
        text_sha256=snapshot['text_sha256'], content_sha256=snapshot['content_sha256'],
        payload_sha256=snapshot['payload_sha256'], snapshot=snapshot, status='queued')
    delivery_id = await session.scalar(statement.on_conflict_do_nothing().returning(MaxPublicationDelivery.id))
    if delivery_id is None:
        return None  # Есть доставка этого источника или точная копия в том же канале.
    for number, part in enumerate(message_parts(snapshot), 1):
        session.add(MaxPublicationPart(delivery_id=delivery_id, number=number, request=part))
    return delivery_id


async def already_observed(session, channel_id, snapshot, *, include_deliveries=True):
    """Одинаковая проверка повторов для предпросмотра и постановки в очередь."""
    observed = await session.scalar(select(MaxObservedMessage.id).where(
        MaxObservedMessage.channel_id == channel_id,
        or_(MaxObservedMessage.source_url == snapshot['source_url'],
            MaxObservedMessage.text_sha256 == text_sha256(snapshot['text']))).limit(1))
    if observed is not None or not include_deliveries:
        return observed is not None
    existing = await session.scalar(select(MaxPublicationDelivery.id).where(
        MaxPublicationDelivery.channel_id == channel_id,
        or_(MaxPublicationDelivery.source_key == snapshot['source_key'],
            MaxPublicationDelivery.content_sha256 == snapshot['content_sha256'])).limit(1))
    return existing is not None


async def queue_new(factory, media_dir):
    async with factory() as session:
        control = await session.get(MaxPublicationControl, 'max')
        if not control or not control.automatic_enabled or not control.enabled_since:
            return
        routes = list((await session.execute(select(MaxPublicationRoute).where(
            MaxPublicationRoute.enabled.is_(True)))).scalars())
        # И дата сообщения, и время получения. История или старый буфер не проходят.
        scope = (select(PipelineEntry.id)
            .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
            .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
            .where(PipelineEntry.stage == 'ready', PipelineEntry.auto_enabled.is_(True),
                   PipelineEntry.created_at >= control.enabled_since, TelegramPost.date >= control.enabled_since,
                   TelegramChat.folder_name == 'MAX', TelegramChat.chat_type == 'channel')
            .order_by(PipelineEntry.id).limit(20))
        entries = list((await session.execute(scope.where(
            PipelineEntry.id > control.scan_after_entry_id))).scalars())
        if not entries:
            entries = list((await session.execute(scope)).scalars())
        if entries:
            control.scan_after_entry_id = entries[-1]
        for entry_id in entries:
            for route in routes:
                try:
                    async with session.begin_nested():
                        await enqueue_delivery(session, entry_id, route, media_dir)
                except PublicationHold as exc:
                    async with session.begin_nested():
                        await record_hold(session, entry_id, route, exc)
                except PreparationError:
                    # Не совпавший маршрут — обычный исход, не ошибка отправителя.
                    pass
        await session.commit()


async def record_history(factory, channel_id, messages):
    async with factory() as session:
        for message in messages:
            body = message.get('body') or {}
            mid, text = body.get('mid'), body.get('text') or ''
            if not mid:
                continue
            # У старых публикаций источник мог быть скрытой ссылкой в markup.
            # Читаем лишь явные URL из ответа MAX, не ищем текст в чужих сервисах.
            links = [item['url'] for item in body.get('markup') or []
                     if isinstance(item, dict) and isinstance(item.get('url'), str)]
            urls = re.findall(r'https://t\.me/(?:c/\d+|[A-Za-z0-9_]+)/\d+', text + '\n' + '\n'.join(links))
            await session.execute(insert(MaxObservedMessage).values(channel_id=channel_id, mid=mid,
                source_url=urls[-1] if urls else None, text_sha256=text_sha256(text)).on_conflict_do_nothing())
        await session.commit()


async def refresh_channel(factory, client, channel_id):
    async with factory() as session:
        channel = await session.get(MaxChannel, channel_id)
        chat_id = channel.chat_id
    access = await client.check_channel(chat_id)
    cursor = None
    while True:
        messages = await client.history(chat_id, cursor)
        await record_history(factory, channel_id, messages)
        if len(messages) < 100:
            break
        # Перекрытие на границе страницы: у нескольких сообщений может быть
        # одна миллисекунда. Минус один здесь терял бы часть таких сообщений.
        next_cursor = min(int(m['timestamp']) for m in messages)
        if cursor is not None and next_cursor >= cursor:
            raise PreparationError('Сверка истории остановилась: повтор страницы MAX')
        cursor = next_cursor
        await asyncio.sleep(0.1)
    async with factory() as session:
        channel = await session.get(MaxChannel, channel_id)
        channel.access_state = 'ok'
        channel.permissions = access['permissions']
        channel.checked_at = channel.history_checked_at = now()
        channel.check_requested = False
        channel.error = None
        if access['title']:
            channel.title = access['title']
        await session.commit()


async def finish_delivery(factory, delivery_id):
    async with factory() as session:
        async with session.begin():
            delivery = await session.get(MaxPublicationDelivery, delivery_id)
            parts = list((await session.execute(select(MaxPublicationPart).where(
                MaxPublicationPart.delivery_id == delivery_id))).scalars())
            parts = [p for p in parts if p.status != 'cancelled']
            if not parts or any(p.status != 'verified' for p in parts):
                return
            delivery.status = 'delivered'
            delivery.delivered_at = now()
            delivery.error = None
            await session.flush()
            others = list((await session.execute(select(MaxPublicationDelivery).where(
                MaxPublicationDelivery.entry_id == delivery.entry_id))).scalars())
            if all(d.status == 'delivered' for d in others):
                entry = await session.get(PipelineEntry, delivery.entry_id, with_for_update=True)
                # Если collector уже отозвал готовность, сохраняем доставку, но не
                # выдаём старую версию за актуальное подготовленное содержимое.
                post = await session.get(TelegramPost, entry.source_post_id)
                if post.is_deleted or text_sha256(post.text) != delivery.text_sha256:
                    delivery.source_changed_at = now()
                entry.stage = 'published'
                entry.status = 'published_source_changed' if delivery.source_changed_at else 'published'
                entry.auto_state = 'done'
            if delivery.batch_id:
                remaining = await session.scalar(select(MaxPublicationDelivery.id).where(
                    MaxPublicationDelivery.batch_id == delivery.batch_id,
                    MaxPublicationDelivery.status != 'delivered').limit(1))
                if not remaining:
                    batch = await session.get(MaxPublicationBatch, delivery.batch_id)
                    batch.status, batch.finished_at = 'complete', now()
