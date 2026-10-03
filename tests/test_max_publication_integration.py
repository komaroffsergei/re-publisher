"""Isolated PostgreSQL outbox checks. No MAX sends or production data."""
from types import SimpleNamespace
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, func

import test_selection_integration as shared
from app.content.post_preparation import mark_source
from app.models import (MaxChannel, MaxObservedMessage, MaxPublicationDelivery, MaxPublicationPart, MaxPublicationRoute,
                        PipelineEntry, TaxonomyClassification, TelegramPost, TelegramChat, SelectionFilterVersion, MaxPublicationControl)
from app.publication.payload import sha_json
from app.publication.service import enqueue_delivery, record_history, refresh_channel, queue_new, prepare
from app.publication.review_guard import GUARD_VERSION, caption_sha
from app.publication.worker import process_one, watch_source_changes
from app.publication.payload import PreparationError
from app.taxonomy.jobs import text_sha256

pytestmark = shared.pytestmark
db, client = shared.db, shared.client


async def ready(factory, client):
    entry_id = await shared.seed(factory)
    mark_id = (await client.post('/api/pipeline/marks', json={'name': 'QA publishing'})).json()['id']
    rule = await shared.save_rule(client, mark_id)
    await shared.flush_apps(factory)
    async with factory() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        chat = await session.get(TelegramChat, post.chat_peer_id)
        await mark_source(session, entry, post, chat)
        entry.stage, entry.status = 'ready', 'ready'
        version = await session.get(SelectionFilterVersion, rule['version_id'])
        channel = MaxChannel(chat_id=-1, title='QA channel', access_state='ok', history_checked_at=shared.datetime.now(shared.timezone.utc))
        session.add(channel); await session.flush()
        route = MaxPublicationRoute(channel_id=channel.id, mark_id=mark_id, filter_id=rule['id'], enabled=True,
            approved_version_id=version.id, quality_gate={'model_key': 'tfidf',
                'expression_sha256': sha_json(version.expression), 'test_matched': 50, 'test_correct': 45,
                'train_positive': 1000, 'model_version': 'qa-v1', 'test_sha256': 'a' * 64, 'split_sha256': 'b' * 64,
                'review_policy': {'version': GUARD_VERSION, 'holds': {}, 'sha256': sha_json({})}})
        session.add(route)
        job = await session.scalar(select(TaxonomyClassification).where(
            TaxonomyClassification.pipeline_entry_id == entry_id, TaxonomyClassification.model_key == 'tfidf'))
        job.model_version = 'qa-v1'
        await session.commit()
        return entry_id, route.id


class FakeMax:
    """Transport fixture only; never used by application runtime."""
    def __init__(self): self.sent = []
    async def identity(self): return 123
    async def send(self, chat_id, text, attachments, reply_mid=None):
        message = {'body': {'mid': 'mid.' + str(len(self.sent) + 1), 'text': text, 'attachments': []},
                   'recipient': {'chat_id': chat_id}, 'url': 'https://max.ru/qa/1'}
        self.sent.append(message)
        return message
    async def get_message(self, mid): return next(m for m in self.sent if m['body']['mid'] == mid)


async def test_review_hold_survives_polling_and_never_creates_send_parts(db, client):
    entry_id, route_id = await ready(db, client)
    async with db() as session:
        route = await session.get(MaxPublicationRoute, route_id)
        holds = {caption_sha('QA sample'): 'Explicit Codex review fixture'}
        route.quality_gate = {**route.quality_gate, 'review_policy': {
            'version': GUARD_VERSION, 'holds': holds, 'sha256': sha_json(holds)}}
        entry = await session.get(PipelineEntry, entry_id)
        entry.auto_enabled = True
        session.add(MaxPublicationControl(name='max', automatic_enabled=True,
            enabled_since=shared.datetime.now(shared.timezone.utc) - timedelta(days=1)))
        await session.commit()
    await queue_new(db, '.')
    await queue_new(db, '.')
    api = FakeMax()
    assert not await process_one(db, api, '.')
    assert not api.sent
    async with db() as session:
        assert await session.scalar(select(func.count(MaxPublicationDelivery.id))) == 1
        delivery = await session.scalar(select(MaxPublicationDelivery))
        assert delivery.status == 'held' and 'Explicit Codex review' in delivery.error
        assert await session.scalar(select(func.count(MaxPublicationPart.id))) == 0
        assert (await session.get(PipelineEntry, entry_id)).stage == 'ready'
        delivery_id = delivery.id
    history = (await client.get(f'/api/publication/history?entry_id={entry_id}')).json()
    assert history['deliveries'][0]['status'] == 'held'
    assert (await client.post(f'/api/publication/deliveries/{delivery_id}/retry', json={})).status_code == 409


async def test_review_policy_change_invalidates_frozen_preview(db, client):
    entry_id, route_id = await ready(db, client)
    async with db() as session:
        route = await session.get(MaxPublicationRoute, route_id)
        old = await prepare(session, entry_id, route, '.')
        holds = {caption_sha('A different reviewed post'): 'Needs review'}
        route.quality_gate = {**route.quality_gate, 'review_policy': {
            'version': GUARD_VERSION, 'holds': holds, 'sha256': sha_json(holds)}}
        with pytest.raises(PreparationError, match='после фиксации'):
            await enqueue_delivery(session, entry_id, route, '.', frozen_sha=old['payload_sha256'])
        assert await session.scalar(select(func.count(MaxPublicationDelivery.id))) == 0


async def test_owner_accepts_failed_model_without_changing_report_or_source_checks(db, client):
    entry_id, route_id = await ready(db, client)
    async with db() as session:
        route = await session.get(MaxPublicationRoute, route_id)
        route.quality_gate = {**route.quality_gate, 'test_correct': 40}
        await session.commit()
        with pytest.raises(PreparationError, match='отложенных совпадений'):
            await prepare(session, entry_id, route, '.')
    response = await client.post(f'/api/publication/routes/{route_id}/accept-current-filter', json={
        'reason': 'Владелец разрешил экспериментальную отправку; модель ещё не прошла допуск.'})
    assert response.status_code == 200
    assert response.json()['admission'] == 'owner_accepted'
    async with db() as session:
        route = await session.get(MaxPublicationRoute, route_id)
        assert route.quality_gate['test_correct'] == 40
        assert route.quality_gate['owner_acceptance']['version_id'] == route.approved_version_id
        await prepare(session, entry_id, route, '.')
        route.approved_version_id = None
        with pytest.raises(PreparationError, match='Изменён фильтр'):
            await prepare(session, entry_id, route, '.')


async def test_outbox_sends_once_and_persists_readback(db, client):
    entry_id, route_id = await ready(db, client)
    async with db() as session:
        route = await session.get(MaxPublicationRoute, route_id)
        delivery_id = await enqueue_delivery(session, entry_id, route, '.')
        assert await enqueue_delivery(session, entry_id, route, '.') is None
        await session.commit()
    api = FakeMax()
    assert await process_one(db, api, '.')
    await process_one(db, api, '.')
    assert len(api.sent) == 1
    async with db() as session:
        assert (await session.get(MaxPublicationDelivery, delivery_id)).status == 'delivered'
        assert (await session.get(PipelineEntry, entry_id)).stage == 'published'
        part = await session.scalar(select(MaxPublicationPart))
        assert part.mid == 'mid.1' and part.verified_at


async def test_two_filters_can_share_label_and_channel_without_duplicate_delivery(db, client):
    entry_id, route_id = await ready(db, client)
    async with db() as session:
        first = await session.get(MaxPublicationRoute, route_id)
        mark_id, channel_id = first.mark_id, first.channel_id
    second_rule = await shared.save_rule(client, mark_id)
    await shared.flush_apps(db)
    async with db() as session:
        first = await session.get(MaxPublicationRoute, route_id)
        version = await session.get(SelectionFilterVersion, second_rule['version_id'])
        second = MaxPublicationRoute(mark_id=mark_id, channel_id=channel_id,
            filter_id=second_rule['id'], enabled=True, approved_version_id=version.id,
            quality_gate={**first.quality_gate, 'expression_sha256': sha_json(version.expression)})
        session.add(second)
        await session.flush()
        assert await enqueue_delivery(session, entry_id, first, '.') is not None
        assert await enqueue_delivery(session, entry_id, second, '.') is None
        await session.commit()
    response = await client.post('/api/publication/routes', json={
        'mark_id': mark_id, 'filter_id': second_rule['id'], 'channel_id': channel_id, 'enabled': False})
    assert response.status_code == 409
    async with db() as session:
        assert await session.scalar(select(func.count(MaxPublicationRoute.id))) == 2
        assert await session.scalar(select(func.count(MaxPublicationDelivery.id))) == 1


async def test_captionless_media_requires_current_matching_ocr_before_publication(db, client, tmp_path):
    """OCR разрешает тему, но в MAX сохраняется исходное вложение без транскрипта."""
    from PIL import Image
    import test_ocr_integration as ocr
    from app.ocr.worker import process, claim
    from app.taxonomy.jobs import enqueue
    from app.taxonomy.worker import finish_job

    entry_id, route_id = await ready(db, client)
    settings = ocr.configure(tmp_path)
    await ocr.media_post(db, entry_id, settings)
    path = tmp_path / 'media' / 'fixture.png'
    Image.new('RGB', (20, 20), 'white').save(path)
    async with db() as session:
        route = await session.get(MaxPublicationRoute, route_id)
        version = await session.get(SelectionFilterVersion, route.approved_version_id)
        version.requires_ocr = True
        version.expression = {'op': 'condition', 'input_source': 'ocr',
                              'label_id': 'is_joke', 'compare': 'gte', 'threshold': 90}
        route.quality_gate = {**route.quality_gate, 'expression_sha256': sha_json(version.expression)}
        job = await enqueue(session, entry_id, 'tfidf', 'taxonomy', 'ocr')
        job_id = job.id
        await session.commit()
    await process(db, settings, ocr.Reader(), *(await claim(db)))
    async with db() as session:
        job = await session.get(TaxonomyClassification, job_id)
        assert job.status == 'queued'
        job.status = 'running'  # Только тестовый транспорт, настоящая очередь выше.
        result = dict(job.result or {})
        result.update(taxonomy_version=shared.taxonomy_catalog()['version'],
                      scores=dict.fromkeys((item['id'] for item in shared.taxonomy_catalog()['labels']), .95),
                      top_3=[], review_status='scored')
        await session.commit()
    await finish_job(db, job_id, result, 10, model_version='qa-v1')
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        await mark_source(session, entry, post, await session.get(TelegramChat, post.chat_peer_id))
        entry.stage, entry.status = 'ready', 'ready'
        await session.commit()
        route = await session.get(MaxPublicationRoute, route_id)
        snapshot = await prepare(session, entry_id, route, settings.media_dir)
        assert snapshot['original_text'] == ''
        assert snapshot['text'] == 'Источник: ' + entry.marked_source_url
        assert len(snapshot['media']) == 1
        assert 'размножаются' not in snapshot['text']
        Image.new('RGB', (20, 20), 'black').save(path)
        with pytest.raises(PreparationError, match='OCR|актуального'):
            await prepare(session, entry_id, route, settings.media_dir)


async def test_restart_with_mid_rechecks_without_sending(db, client):
    entry_id, route_id = await ready(db, client)
    api = FakeMax()
    async with db() as session:
        delivery_id = await enqueue_delivery(session, entry_id, await session.get(MaxPublicationRoute, route_id), '.')
        await session.flush()
        part = await session.scalar(select(MaxPublicationPart))
        message = await api.send(-1, part.request['text'], [])
        part.mid, part.status = message['body']['mid'], 'sent'
        await session.commit()
    await process_one(db, api, '.')
    assert len(api.sent) == 1
    async with db() as session: assert (await session.get(MaxPublicationDelivery, delivery_id)).status == 'delivered'


async def test_interrupted_send_is_unknown_and_not_repeated(db, client):
    entry_id, route_id = await ready(db, client)
    async with db() as session:
        ident = await enqueue_delivery(session, entry_id, await session.get(MaxPublicationRoute, route_id), '.')
        await session.flush()
        part = await session.scalar(select(MaxPublicationPart)); part.status = 'sending'
        await session.commit()
    api = FakeMax()
    await process_one(db, api, '.')
    await process_one(db, api, '.')
    assert not api.sent
    async with db() as session: assert (await session.get(MaxPublicationDelivery, ident)).status == 'unknown'
    response = await client.post(f'/api/publication/deliveries/{ident}/retry', json={})
    assert response.status_code == 409


async def test_owner_api_is_protected_and_cross_origin_actions_are_rejected(db, client):
    assert (await client.get('/api/publication', auth=None)).status_code == 401
    assert (await client.get('/publication')).status_code == 200
    assert (await client.post('/api/publication/automatic', json={'enabled': True}, headers={'Origin': 'https://other.example'})).status_code == 403
    assert (await client.post('/api/publication/automatic', json={'enabled': True})).status_code == 409


async def test_preview_rejects_source_already_hidden_in_channel_markup(db, client):
    entry_id, route_id = await ready(db, client)
    async with db() as session:
        route = await session.get(MaxPublicationRoute, route_id)
        entry = await session.get(PipelineEntry, entry_id)
        channel_id, url = route.channel_id, entry.marked_source_url
    await record_history(db, channel_id, [{'body': {'mid': 'mid.old', 'text': 'Earlier publication',
        'markup': [{'type': 'link', 'url': url}, {'type': 'link', 'url': None}]}}])
    result = await client.post('/api/publication/batches/preview', json={'title': 'QA preview',
        'items': [{'entry_id': entry_id, 'route_id': route_id}]})
    assert result.status_code == 200
    assert result.json()['manifest']['items'] == []
    assert len(result.json()['rejected']) == 1


async def test_two_routes_send_once_each_before_post_is_published(db, client):
    entry_id, route_id = await ready(db, client)
    async with db() as session:
        first = await session.get(MaxPublicationRoute, route_id)
        second_channel = MaxChannel(chat_id=-2, title='QA second', access_state='ok',
            history_checked_at=shared.datetime.now(shared.timezone.utc))
        session.add(second_channel); await session.flush()
        second = MaxPublicationRoute(channel_id=second_channel.id, filter_id=first.filter_id,
            mark_id=first.mark_id, approved_version_id=first.approved_version_id,
            quality_gate=first.quality_gate, enabled=True)
        session.add(second); await session.flush()
        await enqueue_delivery(session, entry_id, first, '.')
        await enqueue_delivery(session, entry_id, second, '.')
        await session.commit()
    api = FakeMax()
    await process_one(db, api, '.')
    async with db() as session: assert (await session.get(PipelineEntry, entry_id)).stage == 'ready'
    await process_one(db, api, '.')
    await process_one(db, api, '.')
    assert len(api.sent) == 2
    assert {m['recipient']['chat_id'] for m in api.sent} == {-1, -2}
    async with db() as session: assert (await session.get(PipelineEntry, entry_id)).stage == 'published'


async def test_changed_source_stops_pending_send_and_is_shown_after_delivery(db, client):
    entry_id, route_id = await ready(db, client)
    async with db() as session:
        delivery_id = await enqueue_delivery(session, entry_id, await session.get(MaxPublicationRoute, route_id), '.')
        await session.commit()
    api = FakeMax()
    await process_one(db, api, '.')
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        post.text += ' Updated source'
        await session.commit()
    await watch_source_changes(db)
    await process_one(db, api, '.')
    assert len(api.sent) == 1
    async with db() as session:
        assert (await session.get(MaxPublicationDelivery, delivery_id)).source_changed_at
    history = (await client.get(f'/api/publication/history?entry_id={entry_id}')).json()
    assert history['deliveries'][0]['source_changed_at']


async def test_history_page_overlap_preserves_equal_timestamps(db, client):
    _, route_id = await ready(db, client)
    async with db() as session:
        channel_id = (await session.get(MaxPublicationRoute, route_id)).channel_id
    def message(i, stamp): return {'timestamp': stamp, 'body': {'mid': f'mid.{i}', 'text': 'QA'}}
    class HistoryMax:
        def __init__(self): self.cursors = []
        async def check_channel(self, chat_id): return {'permissions': ['write', 'read_all_messages'], 'title': 'QA'}
        async def history(self, chat_id, cursor=None):
            self.cursors.append(cursor)
            return [message(i, 2000 - i) for i in range(100)] if cursor is None else [message(99, 1901), message(100, 1901), message(101, 1900)]
    api = HistoryMax()
    await refresh_channel(db, api, channel_id)
    assert api.cursors == [None, 1901]
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(MaxObservedMessage)) == 102
