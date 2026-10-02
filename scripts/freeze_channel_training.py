"""Повторы, общее разделение и закрытый снимок ручной разметки.

Сходство текста здесь служит только защите train/test. Метки не выводятся
из сходства, источника, ключевых слов или предсказаний старой модели.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from channel_corpus import CHECKOUT, FEATURES, TOPICS, connect, coverage, normalize, positive_group_counts


def shingles(text):
    words = re.findall(r'\w+', re.sub(r'https?://\S+', ' ', text.lower()))
    return {' '.join(words[i:i + 3]) for i in range(max(1, len(words) - 2))}


def split_for(group_id):
    bucket = int(hashlib.sha256(('20261001:' + group_id).encode()).hexdigest()[:8], 16) % 10000
    return 'train' if bucket < 8000 else 'validation' if bucket < 9000 else 'test'


def reserve_holdout(db, paths):
    """Старая контрольная сотня остаётся контрольной, включая её близкие копии."""
    expected_hashes = set()
    with db:
        for path in paths:
            for line in path.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                db.execute('INSERT OR IGNORE INTO reserved_sources VALUES (?,?)',
                           (int(row['chat_peer_id']), int(row['message_id'])))
                if row.get('text_sha256'):
                    expected_hashes.add(row['text_sha256'])
        # Старый отпечаток считался от strip(), новый — от нормализованных
        # пробелов. Сопоставляем текст, а не предполагаем равенство хешей.
        if expected_hashes:
            for row in db.execute('SELECT sha,text FROM texts WHERE length>0'):
                if hashlib.sha256(row['text'].strip().encode()).hexdigest() in expected_hashes:
                    db.execute('INSERT OR IGNORE INTO reserved_texts VALUES (?)', (row['sha'],))
        db.execute('INSERT OR IGNORE INTO reserved_texts SELECT s.sha FROM sources s '
                   'JOIN reserved_sources r ON r.peer=s.peer AND r.message=s.message')


def group_and_split(db, reserved_paths=(), manual_group_paths=()):
    reserve_holdout(db, reserved_paths)
    rows = db.execute('SELECT sha,text FROM texts t WHERE length>0 AND EXISTS(SELECT 1 FROM sources s WHERE s.sha=t.sha) ORDER BY sha').fetchall()
    parent = {r['sha']: r['sha'] for r in rows}
    def root(key):
        if parent[key] != key:
            parent[key] = root(parent[key])
        return parent[key]
    def join(a, b):
        a, b = root(a), root(b)
        parent[max(a, b)] = min(a, b)
    # Codex отдельно прочитал эти пересказы. Связь защищает разделение,
    # но не переносит метки между текстами. Сохраняем её для перезапусков.
    with db:
        for path in manual_group_paths:
            for line in path.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                members = item.get('texts')
                if (not isinstance(members, list) or len(set(members)) < 2
                    or not isinstance(item.get('reason'), str) or not item['reason'].strip()
                    or any(not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha)
                           or sha not in parent for sha in members)):
                    raise ValueError('Invalid manually reviewed repeat group')
                first, *rest = sorted(set(members))
                for other in rest:
                    db.execute('INSERT INTO manual_repeat_links VALUES (?,?,?,?) '
                               'ON CONFLICT(left_sha,right_sha) DO UPDATE SET reason=excluded.reason,batch=excluded.batch',
                               (first, other, item['reason'], path.name))
    for link in db.execute('SELECT left_sha,right_sha FROM manual_repeat_links'):
        if link['left_sha'] in parent and link['right_sha'] in parent:
            join(link['left_sha'], link['right_sha'])
    buckets, sets = defaultdict(list), {}
    # Варианты подписи одной исходной записи никогда не разделяем.
    # Ключ источника в sources актуален; старые версии без источника исключены.
    for row in rows:
        sha, text = row['sha'], row['text']
        tokens = shingles(text)
        sets[sha] = tokens
        if len(tokens) < 8:
            # Короткие общие фразы не связываем по одному слову.
            continue
        # MinHash без зависимостей. 24 независимых salt, 8 полос по 3 значения.
        raw = [int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(), 'big') for t in tokens]
        prime = (1 << 61) - 1
        sketch = [min(((salt * 104729 + 15485863) * h + salt * 32452843) % prime for h in raw) for salt in range(1, 25)]
        candidates = set()
        bands = [(i, tuple(sketch[i * 3:i * 3 + 3])) for i in range(8)]
        for key in bands:
            candidates.update(buckets[key])
        for other in candidates:
            previous = sets[other]
            if min(len(tokens), len(previous)) / max(len(tokens), len(previous)) >= .8 and len(tokens & previous) / len(tokens | previous) >= .8:
                join(sha, other)
        for key in bands:
            buckets[key].append(sha)
    reserved = {r['sha'] for r in db.execute('SELECT sha FROM reserved_texts')}
    reserved_groups = {root(sha) for sha in reserved if sha in parent}
    with db:
        for sha in parent:
            group_id = root(sha)
            split = 'test' if group_id in reserved_groups else split_for(group_id)
            db.execute('UPDATE texts SET group_id=?,split=? WHERE sha=?', (group_id, split, sha))
    return {'texts': len(rows), 'groups': len({root(k) for k in parent}),
            'reserved_test_groups': len(reserved_groups), **coverage(db)}


def taxonomy():
    result = json.loads((CHECKOUT / 'config/max_taxonomy.json').read_text(encoding='utf-8'))
    result['version'] = TOPICS['version']
    result['binary_features'] = list(dict.fromkeys(result['binary_features'] + FEATURES))
    result['feature_names'] = {t['id']: t['name'] for t in TOPICS['topics']}
    result['feature_names']['caption_has_context'] = 'Подпись даёт контекст'
    return result


def freeze(db, output, legacy_labels=None, legacy_worklist=None, legacy_audit=None):
    if output.resolve().is_relative_to(CHECKOUT):
        raise ValueError('Dataset must stay outside Git')
    records = {}
    for row in db.execute('SELECT t.*,a.labels_json,a.reasons_json,a.reviewed FROM texts t JOIN annotations a ON a.sha=t.sha WHERE EXISTS(SELECT 1 FROM sources s WHERE s.sha=t.sha)'):
        source_rows = list(db.execute('SELECT * FROM sources WHERE sha=?', (row['sha'],)))
        records[row['sha']] = {'sha': row['sha'], 'text': row['text'], 'group_id': row['group_id'],
            'split': row['split'], 'labels': json.loads(row['labels_json']), 'reasons': json.loads(row['reasons_json']),
            'complexity': None, 'provenance': 'codex_agent', 'reviewed': bool(row['reviewed']),
            'publication_allowed': any(s['chat_type'] == 'channel' for s in source_rows),
            'source_chats': sorted({s['peer'] for s in source_rows}), 'short_caption': len(normalize(row['text'])) <= 180}
    if any(not r['split'] for r in records.values()):
        raise ValueError('Assign repeat groups and splits before freezing')
    reserved_groups = {r['group_id'] for r in db.execute('SELECT t.group_id FROM texts t '
                       'JOIN reserved_texts r ON r.sha=t.sha')}
    if any(r['group_id'] in reserved_groups and r['split'] != 'test' for r in records.values()):
        raise ValueError('Previously reserved holdout material must remain in test')
    if legacy_labels:
        audit = json.loads(legacy_audit.read_text(encoding='utf-8')) if legacy_audit else {}
        if not audit.get('accepted'):
            raise ValueError('Old annotations require a saved Codex audit before reuse')
        for path, key in [(legacy_labels, 'labels_sha256'), (legacy_worklist, 'worklist_sha256')]:
            if not path or hashlib.sha256(path.read_bytes()).hexdigest() != audit.get(key):
                raise ValueError('Legacy audit does not match its frozen inputs')
        excluded_features = set(audit.get('excluded_features', []))
        if 'is_joke' not in excluded_features:
            raise ValueError('Legacy humour labels must not override the fresh caption audit')
        overrides = audit.get('reviewed_overrides', {})
        excluded_ids = set(audit.get('excluded_ids', []))
        min_chars = audit.get('minimum_unreviewed_characters')
        if not isinstance(min_chars, int) or min_chars < 180:
            raise ValueError('Legacy reuse needs an explicit conservative input selection')
        worklist = {int(r['id']): r for r in map(json.loads, legacy_worklist.read_text(encoding='utf-8').splitlines()) if r}
        old_taxonomy = json.loads((CHECKOUT / 'config/max_taxonomy.json').read_text(encoding='utf-8'))
        old_names = [c['id'] for c in old_taxonomy['categories']]
        old_names += [s['id'] for c in old_taxonomy['categories'] for s in c['subcategories']]
        old_names += old_taxonomy['binary_features']
        if excluded_features - set(old_names):
            raise ValueError('Unknown legacy feature exclusion')
        accepted = 0
        for label in map(json.loads, legacy_labels.read_text(encoding='utf-8').splitlines()):
            if label['needs_review'] or str(label['id']) in excluded_ids:
                continue
            if label.get('all_other_labels') != 'нет' or label.get('taxonomy_version') != old_taxonomy['version']:
                raise ValueError('Old label does not explicitly define remaining labels/version')
            source = worklist[int(label['id'])]
            if source['partition_hint'] != 'development':
                raise ValueError('Reserved blind/comparison texts cannot enter training')
            if label['text_sha256'] != hashlib.sha256(source['text'].strip().encode()).hexdigest():
                raise ValueError('Old annotation text changed')
            sha = hashlib.sha256(normalize(source['text']).encode()).hexdigest()
            # Это отбор уже размеченных человеком/агентом входов, а не
            # вычисление меток по длине. Сомнительные короткие реплики не
            # подмешиваются в прежние heads автоматически.
            if len(normalize(source['text'])) < min_chars and str(label['id']) not in overrides:
                continue
            if sha in records and records[sha]['labels'].get('caption_has_context') != 'yes':
                continue
            row = db.execute('SELECT * FROM texts WHERE sha=?', (sha,)).fetchone()
            if not row or not db.execute('SELECT 1 FROM sources WHERE sha=?', (sha,)).fetchone():
                continue
            values = {name: 'yes' if name in label['yes'] else 'unclear' if name in label['unclear'] else 'no'
                      for name in old_names if name not in excluded_features}
            override = overrides.get(str(label['id']), {})
            for name, value in override.get('labels', {}).items():
                if name not in old_names or value not in {'yes', 'no', 'unclear'}:
                    raise ValueError('Invalid explicit Codex legacy correction')
                if name not in excluded_features:
                    values[name] = value
            if sha not in records:
                source_rows = list(db.execute('SELECT * FROM sources WHERE sha=?', (sha,)))
                records[sha] = {'sha': sha, 'text': row['text'], 'group_id': row['group_id'], 'split': row['split'],
                    'labels': {}, 'reasons': {}, 'complexity': None, 'provenance': 'codex_agent_legacy_audited',
                    'reviewed': False, 'publication_allowed': any(s['chat_type'] == 'channel' for s in source_rows),
                    'source_chats': sorted({s['peer'] for s in source_rows}), 'short_caption': len(normalize(row['text'])) <= 180}
            # Свежая ручная разметка шутки имеет приоритет. Отсутствующие новые
            # признаки остаются неизвестными, а не отрицательными.
            for name, value in values.items():
                records[sha]['labels'].setdefault(name, value)
            records[sha]['complexity'] = override.get('technical_complexity', label.get('technical_complexity'))
            accepted += 1
    counts = defaultdict(Counter)
    groups = defaultdict(set)
    for r in records.values():
        groups[r['split']].add(r['group_id'])
        for name, value in r['labels'].items():
            if value == 'yes':
                counts[r['split']][name] += 1
    if any(groups[a] & groups[b] for a, b in [('train', 'validation'), ('train', 'test'), ('validation', 'test')]):
        raise ValueError('Repeat group crosses dataset partitions')
    group_counts = positive_group_counts(records.values())
    training_groups = group_counts.get('train', {})
    missing = {t['id']: 1000 - training_groups.get(t['id'], 0) for t in TOPICS['topics']
               if training_groups.get(t['id'], 0) < 1000}
    if missing:
        raise ValueError('Training quotas not reached: ' + json.dumps(missing))
    result = sorted(records.values(), key=lambda r: r['sha'])
    encoded = ''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in result).encode()
    output.mkdir(parents=True, exist_ok=False)
    (output / 'dataset.jsonl').write_bytes(encoded)
    (output / 'taxonomy.json').write_text(json.dumps(taxonomy(), ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = {'version': TOPICS['version'], 'dataset_sha256': hashlib.sha256(encoded).hexdigest(),
        'rows': len(result), 'positive_by_split': dict(counts), 'positive_groups_by_split': group_counts,
        'annotation': 'Codex; agreement with agent, not independent human accuracy',
        'split_seed': 20261001, 'repeat_group_overlap': 0, 'input': 'text_only'}
    if legacy_labels:
        manifest['legacy_compatibility'] = {'rows_reused': accepted,
            'audit_sha256': hashlib.sha256(legacy_audit.read_bytes()).hexdigest(),
            'excluded_features': sorted(excluded_features),
            'review_method': audit.get('review_method'),
            'individually_rechecked': audit.get('individually_rechecked'),
            'limitations': audit.get('limitations')}
    (output / 'dataset-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--group', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--legacy-labels', type=Path)
    parser.add_argument('--legacy-worklist', type=Path)
    parser.add_argument('--legacy-audit', type=Path)
    parser.add_argument('--reserved-holdout', type=Path, action='append', default=[])
    parser.add_argument('--manual-groups', type=Path, action='append', default=[])
    args = parser.parse_args()
    db = connect(args.directory)
    try:
        result = group_and_split(db, args.reserved_holdout, args.manual_groups) if args.group else freeze(db, args.output, args.legacy_labels, args.legacy_worklist, args.legacy_audit)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        db.close()
