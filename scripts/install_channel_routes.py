"""Перенос проверенного отчёта обучения в девять версионных фильтров MAX.

Без --commit только проверяет файлы и показывает пороги. Не запускает
классификацию старого буфера, не создаёт доставки, не включает отправитель.
Старые версии фильтров сохраняются. Ключ бота этому скрипту не нужен.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select, text

from app.config import get_settings
from app.content.selection_rules import taxonomy_catalog, validate_expression
from app.db import create_engine, create_session_factory
from app.models import FilterMark, MaxChannel, MaxPublicationRoute, SelectionFilter, SelectionFilterVersion
from app.publication.payload import sha_json
from app.publication.service import gate_error
from app.publication.review_guard import policy_error
from app.taxonomy.artifact import artifact_version, configured_artifact, checkpoint_path

ROOT = Path(__file__).resolve().parents[1]


def checked_routes(bundle: Path, review_policy=None, *, allow_partial=False):
    topics = json.loads((ROOT / 'config/max_channel_topics.json').read_text(encoding='utf-8'))
    report = json.loads((bundle / 'evaluation.json').read_text(encoding='utf-8'))
    taxonomy = json.loads((bundle / 'taxonomy.json').read_text(encoding='utf-8'))
    if taxonomy['version'] != taxonomy_catalog()['version']:
        raise ValueError('Runtime taxonomy differs from the trained artifact')
    if not report.get('all_routes_ready') and not allow_partial:
        raise ValueError('Not all nine publication routes passed the held-out checks')
    versions = {}
    for key in ['tfidf', 'minilm']:
        artifact = configured_artifact(bundle, key)
        if key == 'minilm':
            training = json.loads((artifact / 'training.json').read_text(encoding='utf-8'))
            artifact = checkpoint_path(artifact, training)
        versions[key] = artifact_version(bundle, key, artifact)
    if error := policy_error(review_policy):
        raise ValueError(error)
    result = []
    for topic in topics['topics']:
        gate = dict(report['routes'].get(topic['id'], {}))
        if gate.get('enabled') is not True:
            if not allow_partial:
                raise ValueError('Unverified route: ' + topic['id'])
            # Отсутствие порога не заменяем нулём. Непрошедшая тема не
            # получает новую версию условия и не допускается к отправке.
            reason = ('Не пройдена проверка отложенных совпадений' if gate else
                      'Validation не дал проверенный маршрут')
            result.append({'topic': topic, 'gate': gate, 'expression': None,
                           'enabled': False, 'reason': reason})
            continue
        if gate.get('model_key') not in versions:
            raise ValueError('Unverified route: ' + topic['id'])
        if gate.get('model_version') != versions[gate['model_key']]:
            raise ValueError('Evaluation was not made with the supplied model weights')
        thresholds = [gate.get('threshold'), gate.get('context_threshold')]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in thresholds):
            raise ValueError('Invalid route thresholds')
        expression = validate_expression({'op': 'and', 'children': [
            {'op': 'condition', 'label_id': topic['id'], 'compare': 'gte', 'threshold': round(thresholds[0] * 100, 8)},
            {'op': 'condition', 'label_id': 'caption_has_context', 'compare': 'gte', 'threshold': round(thresholds[1] * 100, 8)}]})
        gate['expression_sha256'] = sha_json(expression)
        gate['review_policy'] = review_policy
        # Ту же проверку использует отправитель перед публикацией.
        from types import SimpleNamespace
        error = gate_error(SimpleNamespace(quality_gate=gate, approved_version_id=1),
                           SimpleNamespace(id=1, expression=expression, model_key=gate['model_key']))
        if error:
            raise ValueError(error)
        result.append({'topic': topic, 'gate': gate, 'expression': expression, 'enabled': True})
    if not any(row['enabled'] for row in result):
        raise ValueError('No publication routes passed the held-out checks')
    return result


async def install(rows):
    settings = get_settings().model_copy(update={'db_pool_size': 1, 'db_max_overflow': 0})
    engine = create_engine(settings)
    factory = create_session_factory(engine=engine)
    changes = []
    try:
        async with factory() as session, session.begin():
            await session.execute(text('SELECT pg_advisory_xact_lock(782497312091)'))
            for row in rows:
                topic, gate, expression = row['topic'], row['gate'], row['expression']
                mark = await session.scalar(select(FilterMark).where(FilterMark.name == topic['mark_name']))
                if mark is None:
                    mark = FilterMark(name=topic['mark_name'], description='Маршрут MAX: ' + topic['name'], color='#fb923c')
                    session.add(mark); await session.flush()
                if mark.archived or mark.label_id:
                    raise ValueError('A dictionary label must be active and independent of model features')
                channel = await session.scalar(select(MaxChannel).where(MaxChannel.chat_id == int(topic['chat_id'])))
                if channel is None:
                    channel = MaxChannel(chat_id=int(topic['chat_id']), title=topic['name'], public_url=topic['url'])
                    session.add(channel); await session.flush()
                route = await session.scalar(select(MaxPublicationRoute).where(
                    MaxPublicationRoute.channel_id == channel.id, MaxPublicationRoute.mark_id == mark.id))
                rule = await session.get(SelectionFilter, route.filter_id) if route else None
                if rule is None and topic['id'] == 'is_joke':
                    candidates = list((await session.execute(select(SelectionFilter)
                        .join(SelectionFilterVersion, SelectionFilterVersion.id == SelectionFilter.active_version_id)
                        .where(SelectionFilterVersion.mark_id == mark.id, SelectionFilter.archived.is_(False)))).scalars())
                    if len(candidates) > 1:
                        raise ValueError('Several existing joke filters; refuse to select one arbitrarily')
                    rule = candidates[0] if candidates else None
                if not row['enabled']:
                    # Старые версии, присвоенные лейблы и история остаются.
                    # Меняем только связанный с этой темой автоматический фильтр.
                    if rule is not None:
                        rule.enabled = False
                    if route is not None:
                        route.enabled = False
                        route.quality_gate = {**gate, 'enabled': False, 'reason': row['reason']}
                    changes.append({'topic': topic['id'], 'enabled': False, 'reason': row['reason'],
                        'filter_id': rule.id if rule else None, 'channel_id': str(channel.chat_id),
                        'old_buffer_queued': 0})
                    continue
                if rule is None:
                    rule = SelectionFilter(name=topic['mark_name'])
                    session.add(rule); await session.flush()
                previous = await session.get(SelectionFilterVersion, rule.active_version_id) if rule.active_version_id else None
                unchanged = previous and previous.model_key == gate['model_key'] and previous.expression == expression and previous.mark_id == mark.id
                if unchanged:
                    version = previous
                else:
                    version = SelectionFilterVersion(filter_id=rule.id, number=previous.number + 1 if previous else 1,
                        name=topic['mark_name'], model_key=gate['model_key'], mark_id=mark.id, expression=expression)
                    session.add(version); await session.flush()
                    rule.active_version_id = version.id
                rule.enabled, rule.archived = True, False
                if route is None:
                    route = MaxPublicationRoute(channel_id=channel.id, filter_id=rule.id, mark_id=mark.id)
                    session.add(route)
                route.enabled, route.approved_version_id, route.quality_gate = True, version.id, gate
                channel.check_requested = True
                changes.append({'topic': topic['id'], 'enabled': True, 'filter_id': rule.id, 'version_id': version.id,
                    'channel_id': str(channel.chat_id), 'model': gate['model_version'], 'old_buffer_queued': 0})
        return changes
    finally:
        await engine.dispose()


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--review-policy', type=Path, required=True,
                        help='Protected explicit Codex publication holds; no raw posts in Git')
    parser.add_argument('--commit', action='store_true')
    parser.add_argument('--allow-partial', action='store_true',
                        help='Install passed topics; disable failed topics without inventing thresholds')
    args = parser.parse_args()
    rows = checked_routes(args.bundle, json.loads(args.review_policy.read_text(encoding='utf-8')),
                          allow_partial=args.allow_partial)
    result = asyncio.run(install(rows)) if args.commit else [
        {'topic': r['topic']['id'], 'enabled': r['enabled'],
         'model': r['gate'].get('model_version'), 'expression': r['expression'],
         'reason': r.get('reason')} for r in rows]
    print(json.dumps({'committed': args.commit, 'routes': result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
