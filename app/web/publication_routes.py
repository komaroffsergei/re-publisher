"""Закрытое управление MAX. В web нет ключа бота и upload-токенов."""
from collections import Counter
from datetime import timedelta
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert

from app.models import (FilterMark, MaxChannel, MaxPublicationBatch, MaxPublicationControl,
                        MaxPublicationDelivery, MaxPublicationPart, MaxPublicationRoute,
                        SelectionFilterVersion, SelectionFilter, ServiceRuntime)
from app.publication.payload import PreparationError, message_parts, sha_json
from app.publication.service import already_observed, enqueue_delivery, gate_error, now, prepare, route_version
from app.web.selection_routes import input_data, EmptyInput


class ChannelInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chat_id: int = Field(lt=0)
    title: str = Field(min_length=1, max_length=150)
    public_url: str = Field(pattern=r'^https://max\.ru/[a-zA-Z0-9_-]+$', max_length=200)


class RouteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mark_id: int = Field(gt=0)
    filter_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    enabled: bool = False


class SelectionItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entry_id: int = Field(gt=0)
    route_id: int = Field(gt=0)
    payload_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    reviewed: bool = False


class BatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=150)
    items: list[SelectionItem] = Field(min_length=1, max_length=180)


class StartInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    manifest_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class AutomaticInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


class OwnerAcceptanceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str = Field(min_length=10, max_length=500)


class ConfirmAbsenceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed_absent: bool
    scan_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def route_json(route):
    return {'id': route.id, 'filter_id': route.filter_id, 'mark_id': route.mark_id,
            'channel_id': route.channel_id, 'enabled': route.enabled,
            'approved_version_id': route.approved_version_id, 'quality_gate': route.quality_gate}


def require_initial_manifest(manifest):
    items = manifest['items']
    counts = Counter(i['channel_id'] for i in items)
    if len(items) != 180 or len(counts) != 9 or any(c != 20 for c in counts.values()):
        raise PreparationError('Первая партия: ровно 20 новых постов в каждом из девяти каналов')
    if any(not i.get('reviewed') for i in items):
        raise PreparationError('Каждая карточка партии должна быть прочитана и проверена')
    if len({(i['channel_id'], i['source_key']) for i in items}) != len(items):
        raise PreparationError('В партии повторяется источник для одного канала')
    if len({(i['channel_id'], i['content_sha256']) for i in items}) != len(items):
        raise PreparationError('В партии повторяется содержимое для одного канала')
    if all('chat_id' in item for item in items):
        topics = json.loads((Path(__file__).resolve().parents[2] / 'config/max_channel_topics.json').read_text(encoding='utf-8'))
        if {str(item['chat_id']) for item in items} != {t['chat_id'] for t in topics['topics']}:
            raise PreparationError('Первая партия должна использовать девять каналов из плана')


async def make_preview(session, data, media_dir):
    accepted, rejected = [], []
    for item in data.items:
        try:
            route = await session.get(MaxPublicationRoute, item.route_id)
            if not route:
                raise PreparationError('Маршрут не найден')
            snapshot = await prepare(session, item.entry_id, route, media_dir)
            if await already_observed(session, route.channel_id, snapshot):
                raise PreparationError('Этот источник или содержимое уже есть в канале либо в очереди доставки')
            channel = await session.get(MaxChannel, route.channel_id)
            if len(message_parts(snapshot)) != 1:
                raise PreparationError('В первую партию подходит только одна публикация MAX без разбиения')
            if item.payload_sha256 and item.payload_sha256 != snapshot['payload_sha256']:
                raise PreparationError('Предпросмотр устарел: карточка изменилась')
            accepted.append({'entry_id': item.entry_id, 'route_id': route.id, 'channel_id': route.channel_id, 'chat_id': str(channel.chat_id),
                'source_key': snapshot['source_key'], 'content_sha256': snapshot['content_sha256'],
                'payload_sha256': snapshot['payload_sha256'], 'source_url': snapshot['source_url'],
                'text': snapshot['text'], 'media_count': len(snapshot['media']), 'reviewed': item.reviewed})
        except PreparationError as exc:
            rejected.append({'entry_id': item.entry_id, 'route_id': item.route_id, 'error': str(exc)})
    manifest = {'title': data.title, 'items': accepted}
    return {'manifest': manifest, 'manifest_sha256': sha_json(manifest), 'rejected': rejected}


def register_publication_routes(app, require_auth, session_factory, templates):
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.get('/publication', response_class=HTMLResponse)
    async def page(request: Request):
        return templates.TemplateResponse(request, 'max_publication.html', {})

    @router.get('/api/publication')
    async def overview(request: Request):
        async with session_factory(request)() as session:
            channels = list((await session.execute(select(MaxChannel).order_by(MaxChannel.id))).scalars())
            routes = list((await session.execute(select(MaxPublicationRoute).order_by(MaxPublicationRoute.id))).scalars())
            marks = {m.id: m for m in (await session.execute(select(FilterMark))).scalars()}
            filters = {f.id: f for f in (await session.execute(select(SelectionFilter))).scalars()}
            control = await session.get(MaxPublicationControl, 'max')
            runtime = await session.get(ServiceRuntime, 'publication-worker')
            counts = (await session.execute(select(MaxPublicationDelivery.status, func.count()).group_by(MaxPublicationDelivery.status))).all()
            return {'sender_enabled': request.app.state.settings.max_publisher_enabled,
                'automatic_enabled': bool(control and control.automatic_enabled),
                'enabled_since': control.enabled_since if control else None,
                'runtime': {'heartbeat': runtime.heartbeat_at, 'error': runtime.error} if runtime else None,
                'counts': dict(counts), 'routes': [{**route_json(r),
                    'filter_name': filters[r.filter_id].name if r.filter_id in filters else None,
                    'mark_name': marks[r.mark_id].name if r.mark_id in marks else None} for r in routes],
                'marks': [{'id': m.id, 'name': m.name} for m in marks.values() if not m.archived],
                'filters': [{'id': f.id, 'name': f.name} for f in filters.values() if not f.archived],
                'channels': [{'id': c.id, 'chat_id': str(c.chat_id), 'title': c.title, 'url': c.public_url,
                    'access': c.access_state, 'permissions': c.permissions, 'checked_at': c.checked_at,
                    'history_checked_at': c.history_checked_at, 'error': c.error,
                    'check_requested': c.check_requested} for c in channels]}

    @router.post('/api/publication/channels')
    async def save_channel(request: Request):
        data = await input_data(request, ChannelInput)
        async with session_factory(request)() as session:
            channel = await session.scalar(select(MaxChannel).where(MaxChannel.chat_id == data.chat_id))
            if channel is None:
                channel = MaxChannel(chat_id=data.chat_id, title=data.title, public_url=data.public_url)
                session.add(channel)
            channel.title, channel.public_url = data.title.strip(), data.public_url
            channel.check_requested = True
            await session.commit()
            return {'id': channel.id}

    @router.post('/api/publication/channels/{channel_id}/check')
    async def check_channel(channel_id: int, request: Request):
        await input_data(request, EmptyInput)
        async with session_factory(request)() as session:
            channel = await session.get(MaxChannel, channel_id)
            if channel is None:
                raise HTTPException(404, 'Канал не найден')
            channel.check_requested = True
            await session.commit()
        return {'queued': True}

    @router.post('/api/publication/routes')
    @router.put('/api/publication/routes/{route_id}')
    async def save_route(request: Request, route_id: int | None = None):
        data = await input_data(request, RouteInput)
        async with session_factory(request)() as session:
            route = await session.get(MaxPublicationRoute, route_id) if route_id else MaxPublicationRoute()
            if route is None:
                raise HTTPException(404, 'Маршрут не найден')
            if (not await session.get(MaxChannel, data.channel_id) or not await session.get(FilterMark, data.mark_id)
                    or not await session.get(SelectionFilter, data.filter_id)):
                raise HTTPException(409, 'Канал, лейбл или фильтр не найден')
            if route_id and (route.filter_id != data.filter_id or route.mark_id != data.mark_id):
                route.approved_version_id, route.quality_gate = None, {}
            for key, value in data.model_dump().items():
                setattr(route, key, value)
            if data.enabled:
                try:
                    await route_version(session, route)
                except PreparationError as exc:
                    raise HTTPException(409, str(exc)) from exc
            session.add(route)
            await session.commit()
            return {'id': route.id}

    @router.post('/api/publication/batches/preview')
    @router.post('/api/publication/batches/freeze')
    async def batch_preview(request: Request):
        data = await input_data(request, BatchInput)
        async with session_factory(request)() as session:
            result = await make_preview(session, data, request.app.state.settings.media_dir)
            if request.url.path.endswith('/freeze'):
                if result['rejected']:
                    raise HTTPException(409, {'error': 'В партии есть недоступные карточки', 'rejected': result['rejected']})
                try:
                    require_initial_manifest(result['manifest'])
                except PreparationError as exc:
                    raise HTTPException(409, str(exc)) from exc
                if any(not item.payload_sha256 for item in data.items):
                    raise HTTPException(409, 'Сначала проверьте предпросмотр и его отпечатки')
                batch = MaxPublicationBatch(title=data.title, manifest=result['manifest'], manifest_sha256=result['manifest_sha256'])
                session.add(batch)
                await session.commit()
                result['batch_id'] = batch.id
            return result

    @router.post('/api/publication/routes/{route_id}/accept-current-filter')
    async def accept_current_filter(route_id: int, request: Request):
        data = await input_data(request, OwnerAcceptanceInput)
        async with session_factory(request)() as session:
            route = await session.get(MaxPublicationRoute, route_id, with_for_update=True)
            if not route:
                raise HTTPException(404, 'Маршрут не найден')
            rule = await session.get(SelectionFilter, route.filter_id)
            version = await session.get(SelectionFilterVersion, rule.active_version_id) if rule else None
            if not rule or not rule.enabled or rule.archived or not version or version.mark_id != route.mark_id:
                raise HTTPException(409, 'Сначала включи связанный фильтр с тем же лейблом')
            gate = dict(route.quality_gate or {})
            if version.model_key != gate.get('model_key') or not gate.get('model_version'):
                raise HTTPException(409, 'Для смены модели сначала закрепи её фактический артефакт в маршруте')
            gate.update(expression_sha256=sha_json(version.expression), owner_acceptance={
                'version_id': version.id, 'accepted_at': now().isoformat(), 'reason': data.reason.strip()})
            route.quality_gate, route.approved_version_id = gate, version.id
            if error := gate_error(route, version):
                raise HTTPException(409, error)
            await session.commit()
            return {'route_id': route.id, 'version_id': version.id, 'admission': 'owner_accepted'}

    @router.get('/api/publication/batches')
    async def batches(request: Request):
        async with session_factory(request)() as session:
            rows = list((await session.execute(select(MaxPublicationBatch).order_by(MaxPublicationBatch.id.desc()).limit(20))).scalars())
            return {'batches': [{'id': b.id, 'title': b.title, 'status': b.status,
                'manifest_sha256': b.manifest_sha256, 'count': len(b.manifest['items']),
                'started_at': b.started_at, 'finished_at': b.finished_at} for b in rows]}

    @router.post('/api/publication/batches/{batch_id}/start')
    async def batch_start(batch_id: int, request: Request):
        data = await input_data(request, StartInput)
        if not request.app.state.settings.max_publisher_enabled:
            raise HTTPException(409, 'Отправитель выключен в конфигурации runtime')
        async with session_factory(request)() as session:
            batch = await session.get(MaxPublicationBatch, batch_id, with_for_update=True)
            if not batch or batch.status != 'prepared' or data.manifest_sha256 != batch.manifest_sha256:
                raise HTTPException(409, 'Партия уже запущена или отпечаток отличается')
            try:
                require_initial_manifest(batch.manifest)
                for item in batch.manifest['items']:
                    route = await session.get(MaxPublicationRoute, item['route_id'])
                    ident = await enqueue_delivery(session, item['entry_id'], route,
                        request.app.state.settings.media_dir, batch_id, item['payload_sha256'])
                    if ident is None:
                        raise PreparationError('В этой партии уже есть доставка: нужен новый состав')
                batch.status, batch.started_at = 'running', now()
                await session.commit()
            except PreparationError as exc:
                raise HTTPException(409, str(exc)) from exc
            return {'queued': 180, 'batch_id': batch_id}

    @router.post('/api/publication/automatic')
    async def automatic(request: Request):
        data = await input_data(request, AutomaticInput)
        async with session_factory(request)() as session:
            if data.enabled:
                if not request.app.state.settings.max_publisher_enabled:
                    raise HTTPException(409, 'Отправитель выключен в runtime')
                completed = list((await session.execute(select(MaxPublicationBatch).where(MaxPublicationBatch.status == 'complete'))).scalars())
                if not any(len(b.manifest['items']) == 180 for b in completed):
                    raise HTTPException(409, 'Сначала требуется подтверждённая первая партия из 180 публикаций')
                routes = list((await session.execute(select(MaxPublicationRoute).where(MaxPublicationRoute.enabled.is_(True)))).scalars())
                if len({r.channel_id for r in routes}) != 9:
                    raise HTTPException(409, 'Должны быть проверены маршруты всех девяти каналов')
                try:
                    for route in routes:
                        await route_version(session, route)
                except PreparationError as exc:
                    raise HTTPException(409, str(exc)) from exc
            await session.execute(insert(MaxPublicationControl).values(name='max').on_conflict_do_nothing())
            control = await session.get(MaxPublicationControl, 'max', with_for_update=True)
            control.automatic_enabled = data.enabled
            if data.enabled and control.enabled_since is None:
                control.enabled_since = now()
            control.updated_at = now()
            await session.commit()
            return {'enabled': control.automatic_enabled, 'enabled_since': control.enabled_since}

    @router.get('/api/publication/history')
    async def history(request: Request, entry_id: int | None = None):
        async with session_factory(request)() as session:
            statement = select(MaxPublicationDelivery, MaxChannel.title).join(MaxChannel).order_by(MaxPublicationDelivery.id.desc()).limit(100)
            if entry_id is not None:
                statement = statement.where(MaxPublicationDelivery.entry_id == entry_id)
            deliveries = []
            for d, title in (await session.execute(statement)).all():
                parts = list((await session.execute(select(MaxPublicationPart).where(MaxPublicationPart.delivery_id == d.id).order_by(MaxPublicationPart.number))).scalars())
                deliveries.append({'id': d.id, 'entry_id': d.entry_id, 'channel': title,
                    'status': d.status, 'error': d.error, 'delivered_at': d.delivered_at,
                    'source_changed_at': d.source_changed_at, 'source_url': d.source_url,
                    'parts': [{'number': p.number, 'status': p.status, 'mid': p.mid,
                        'url': p.public_url, 'verified_at': p.verified_at, 'error': p.error,
                        'absence_scan_count': p.absence_scan_count, 'absence_scan_sha256': p.absence_scan_sha256,
                        'absence_checked_at': p.absence_checked_at} for p in parts]})
            return {'deliveries': deliveries}

    @router.post('/api/publication/deliveries/{delivery_id}/retry')
    async def retry(delivery_id: int, request: Request):
        await input_data(request, EmptyInput)
        async with session_factory(request)() as session:
            delivery = await session.get(MaxPublicationDelivery, delivery_id, with_for_update=True)
            if not delivery or delivery.status != 'failed':
                raise HTTPException(409, 'Повтор допустим только при подтверждённой ошибке; unknown сначала сверяется с каналом')
            parts = list((await session.execute(select(MaxPublicationPart).where(MaxPublicationPart.delivery_id == delivery_id))).scalars())
            if any(p.status in {'sending', 'unknown'} and not p.mid for p in parts):
                raise HTTPException(409, 'Есть неопределённая отправка; повтор может создать дубль')
            if not any(p.mid for p in parts):
                route = await session.get(MaxPublicationRoute, delivery.route_id)
                try:
                    snapshot = await prepare(session, delivery.entry_id, route, request.app.state.settings.media_dir)
                    if snapshot['payload_sha256'] != delivery.payload_sha256:
                        if delivery.batch_id is not None:
                            raise PreparationError('Изменился пост зафиксированной партии; нужно заново проверить состав')
                        delivery.snapshot, delivery.payload_sha256 = snapshot, snapshot['payload_sha256']
                        delivery.text_sha256, delivery.content_sha256 = snapshot['text_sha256'], snapshot['content_sha256']
                        requests = message_parts(snapshot)
                        existing = {p.number: p for p in parts}
                        for number, part in enumerate(requests, 1):
                            if number in existing:
                                saved = existing[number]
                                saved.request, saved.attachments, saved.status = part, [], 'prepared'
                                saved.send_started_at, saved.error = None, None
                            else:
                                session.add(MaxPublicationPart(delivery_id=delivery.id, number=number, request=part))
                        for saved in parts:
                            if saved.number > len(requests):
                                saved.status = 'cancelled'
                except PreparationError as exc:
                    raise HTTPException(409, str(exc)) from exc
            delivery.status, delivery.attempts, delivery.error, delivery.next_attempt_at = 'queued', 0, None, None
            await session.commit()
            return {'queued': True}

    @router.post('/api/publication/deliveries/{delivery_id}/confirm-absence')
    async def confirm_absence(delivery_id: int, request: Request):
        data = await input_data(request, ConfirmAbsenceInput)
        async with session_factory(request)() as session:
            delivery = await session.get(MaxPublicationDelivery, delivery_id, with_for_update=True)
            if not delivery or delivery.status != 'unknown' or not data.confirmed_absent:
                raise HTTPException(409, 'Нужно явное подтверждение отсутствия неопределённой доставки')
            part = await session.scalar(select(MaxPublicationPart).where(
                MaxPublicationPart.delivery_id == delivery_id, MaxPublicationPart.mid.is_(None),
                MaxPublicationPart.status.in_(['unknown', 'sending'])).with_for_update())
            if (not part or part.absence_scan_count < 2 or part.absence_scan_sha256 != data.scan_sha256
                    or not part.absence_checked_at or now() - part.absence_checked_at > timedelta(seconds=90)):
                raise HTTPException(409, 'Нужны две завершённые актуальные сверки канала без совпадения')
            part.status, part.error = 'uploaded', None
            delivery.status, delivery.error, delivery.attempts, delivery.next_attempt_at = 'queued', None, 0, None
            await session.commit()
            return {'queued': True}

    app.include_router(router)
