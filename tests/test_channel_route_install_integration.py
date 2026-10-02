"""Partial release uses disposable PostgreSQL; no model jobs or MAX requests."""
import json

from sqlalchemy import select, func

import test_selection_integration as shared
import install_channel_routes as installer
from test_channel_route_install import bundle, review_policy
from app.models import (FilterMark, FilterMarkEvent, MaxChannel, MaxPublicationControl,
                        MaxPublicationDelivery, MaxPublicationRoute, PipelineEntry, PostFilterMark,
                        SelectionFilter, SelectionFilterVersion, TaxonomyRun, FilterApplication)

pytestmark = shared.pytestmark
db = shared.db


async def test_partial_release_disables_failed_topic_and_preserves_history(db, tmp_path, monkeypatch):
    directory, report = bundle(tmp_path, monkeypatch)
    report['all_routes_ready'] = False
    del report['routes']['is_joke']
    report['routes']['is_ai_beginner_material']['enabled'] = False
    (directory / 'evaluation.json').write_text(json.dumps(report))
    rows = installer.checked_routes(directory, review_policy(), allow_partial=True)
    settings = installer.get_settings().model_copy(update={'db_dsn': shared.DSN})
    monkeypatch.setattr(installer, 'get_settings', lambda: settings)
    entry_id = await shared.seed(db)
    joke = rows[0]['topic']
    assert joke['id'] == 'is_joke'
    async with db() as session:
        mark = FilterMark(name=joke['mark_name'], color='#fb923c')
        other = FilterMark(name='QA unrelated label', color='#ffffff')
        session.add_all([mark, other]); await session.flush()
        rule = SelectionFilter(name='Previous joke filter', enabled=True)
        unrelated = SelectionFilter(name='QA unrelated filter', enabled=True)
        channel = MaxChannel(chat_id=int(joke['chat_id']), title='QA existing channel')
        session.add_all([rule, unrelated, channel]); await session.flush()
        version = SelectionFilterVersion(filter_id=rule.id, number=1, name=rule.name,
            model_key='tfidf', mark_id=mark.id, expression={'op': 'condition',
                'label_id': 'is_joke', 'compare': 'gte', 'threshold': 45})
        session.add(version); await session.flush()
        rule.active_version_id = version.id
        route = MaxPublicationRoute(channel_id=channel.id, mark_id=mark.id, filter_id=rule.id,
            enabled=True, approved_version_id=version.id)
        session.add_all([route, PostFilterMark(entry_id=entry_id, mark_id=mark.id),
            FilterMarkEvent(entry_id=entry_id, mark_id=mark.id, action='assigned', dedup_key='qa-initial-mark')])
        await session.commit()
        old_ids = rule.id, version.id, route.id, unrelated.id, mark.id
    for _ in range(2):
        result = await installer.install(rows)
        assert sum(r['enabled'] for r in result) == 7
        assert all(r['old_buffer_queued'] == 0 for r in result)
    async with db() as session:
        rule_id, version_id, route_id, other_id, mark_id = old_ids
        rule = await session.get(SelectionFilter, rule_id)
        assert not rule.enabled and rule.active_version_id == version_id
        assert not (await session.get(MaxPublicationRoute, route_id)).enabled
        assert (await session.get(SelectionFilter, other_id)).enabled
        assert await session.scalar(select(func.count(SelectionFilterVersion.id))) == 8
        assert await session.scalar(select(func.count(FilterMarkEvent.id))) == 1
        assert await session.scalar(select(PostFilterMark.active).where(
            PostFilterMark.entry_id == entry_id, PostFilterMark.mark_id == mark_id))
        assert (await session.get(PipelineEntry, entry_id)).stage == 'sorted'
        assert await session.scalar(select(func.count(TaxonomyRun.id))) == 2
        assert await session.scalar(select(func.count(FilterApplication.id))) == 0
        assert await session.scalar(select(func.count(MaxPublicationDelivery.id))) == 0
        assert await session.scalar(select(func.count(MaxPublicationControl.name))) == 0
