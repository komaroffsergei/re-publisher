"""Подготовка и проверка ручной разметки Codex. Семантических правил здесь нет.

SQLite — локальный индекс исходников, повторов и сохранённых партий. Каждая
метка приходит явно из прочитанной Codex партии; неразмеченный текст не считается
отрицательным. Точные повторы объединяются, источники остаются отдельными.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
TOPICS = json.loads((CHECKOUT / 'config/max_channel_topics.json').read_text(encoding='utf-8'))
FEATURES = [t['id'] for t in TOPICS['topics']] + [TOPICS['caption_context_feature']]
ALIASES = dict(zip('JEBRPWVCTK', FEATURES, strict=True))


def normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def connect(directory: Path):
    if directory.resolve().is_relative_to(CHECKOUT):
        raise ValueError('Private corpus cannot be inside the checkout')
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / 'corpus.sqlite3')
    db.row_factory = sqlite3.Row
    db.create_function('casefold', 1, str.casefold, deterministic=True)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS texts (
      sha TEXT PRIMARY KEY, text TEXT NOT NULL, length INTEGER NOT NULL,
      has_media INTEGER NOT NULL DEFAULT 0, split TEXT,
      group_id TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sources (
      peer INTEGER NOT NULL, message INTEGER NOT NULL, sha TEXT NOT NULL,
      source_id TEXT, date TEXT, chat_type TEXT, grouped_id TEXT, media INTEGER,
      PRIMARY KEY(peer,message)
    );
    CREATE INDEX IF NOT EXISTS source_sha ON sources(sha);
    CREATE TABLE IF NOT EXISTS annotations (
      sha TEXT PRIMARY KEY REFERENCES texts(sha), version TEXT NOT NULL,
      labels_json TEXT NOT NULL, reasons_json TEXT NOT NULL,
      created_at TEXT NOT NULL, batch TEXT NOT NULL,
      reviewed INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS imports (file TEXT PRIMARY KEY, sha TEXT NOT NULL, rows INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS annotation_history (
      id INTEGER PRIMARY KEY, sha TEXT NOT NULL, old_json TEXT, new_json TEXT NOT NULL,
      created_at TEXT NOT NULL, batch TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS reserved_sources (
      peer INTEGER NOT NULL, message INTEGER NOT NULL, PRIMARY KEY(peer,message)
    );
    CREATE TABLE IF NOT EXISTS reserved_texts (sha TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS manual_repeat_links (
      left_sha TEXT NOT NULL, right_sha TEXT NOT NULL,
      reason TEXT NOT NULL, batch TEXT NOT NULL,
      PRIMARY KEY(left_sha,right_sha)
    );
    ''')
    return db


def import_snapshot(db, path: Path):
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    previous = db.execute('SELECT sha FROM imports WHERE file=?', (str(path.resolve()),)).fetchone()
    if previous:
        if previous['sha'] != checksum:
            raise ValueError('Imported snapshot changed')
        return 0
    opener = gzip.open if path.name.endswith('.gz') else open
    count = 0
    now = datetime.now(timezone.utc).isoformat()
    with opener(path, 'rt', encoding='utf-8') as handle, db:
        for line in handle:
            row = json.loads(line)
            if row.get('is_deleted'):
                continue
            text = normalize(row.get('text', ''))
            sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
            media = bool(row.get('has_media') or row.get('media_type'))
            db.execute('INSERT OR IGNORE INTO texts VALUES (?,?,?,?,?,?,?)',
                       (sha, row.get('text') or '', len(text), media, None, sha, now))
            if media:
                db.execute('UPDATE texts SET has_media=1 WHERE sha=?', (sha,))
            db.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(peer,message) DO UPDATE SET '
                       'sha=excluded.sha,source_id=excluded.source_id,date=excluded.date,'
                       'chat_type=COALESCE(excluded.chat_type,sources.chat_type),grouped_id=excluded.grouped_id,media=excluded.media',
                       (int(row['chat_peer_id']), int(row['message_id']), sha, str(row['id']),
                        row.get('date'), row.get('chat_type'), str(row['grouped_id']) if row.get('grouped_id') else None, media))
            count += 1
        db.execute('INSERT INTO imports VALUES (?,?,?)', (str(path.resolve()), checksum, count))
    return count


def positive_group_counts(rows):
    """Одна семья повторов даёт не более одного примера на признак.

    Число подписей без контекста не увеличивает квоту. Эта функция считает
    только явные метки агента и не переносит их между похожими текстами.
    """
    groups = {}
    for row in rows:
        if row['labels'].get('caption_has_context') != 'yes':
            continue
        split = row['split'] or 'unassigned'
        for name, value in row['labels'].items():
            if value == 'yes':
                groups.setdefault(split, {}).setdefault(name, set()).add(row['group_id'])
    return {split: Counter({name: len(ids) for name, ids in values.items()})
            for split, values in groups.items()}


def coverage(db):
    counts = Counter()
    unclear = Counter()
    splits = {name: Counter() for name in ['train', 'validation', 'test', 'unassigned']}
    quota_rows = []
    for row in db.execute('SELECT a.labels_json,t.split,t.group_id FROM annotations a JOIN texts t ON a.sha=t.sha WHERE EXISTS(SELECT 1 FROM sources s WHERE s.sha=t.sha)'):
        labels = json.loads(row['labels_json'])
        quota_rows.append({'labels': labels, 'split': row['split'], 'group_id': row['group_id']})
        for name, value in labels.items():
            if value == 'yes':
                counts[name] += 1
                splits[row['split'] or 'unassigned'][name] += 1
            elif value == 'unclear':
                unclear[name] += 1
    group_counts = positive_group_counts(quota_rows)
    return {
        'source_messages': db.execute('SELECT COUNT(*) FROM sources').fetchone()[0],
        'unique_texts': db.execute('SELECT COUNT(*) FROM texts t WHERE length>0 AND EXISTS(SELECT 1 FROM sources s WHERE s.sha=t.sha)').fetchone()[0],
        'empty_messages': db.execute('SELECT COUNT(*) FROM sources s JOIN texts t ON s.sha=t.sha WHERE t.length=0').fetchone()[0],
        'annotated_unique': db.execute('SELECT COUNT(*) FROM annotations').fetchone()[0],
        'positive': {name: counts[name] for name in FEATURES},
        'unclear': {name: unclear[name] for name in FEATURES},
        'positive_by_split': {name: dict(value) for name, value in splits.items()},
        'positive_groups_by_split': {name: dict(value) for name, value in group_counts.items()},
        'quota_ready': all(group_counts.get('train', {}).get(t['id'], 0) >= 1000 for t in TOPICS['topics']),
    }


def next_batch(db, size: int, contains: str | None, max_length: int | None, publishable_only: bool = False):
    # Отбор лишь помогает найти кандидатов. Он не назначает меток.
    where = ['a.sha IS NULL', 't.length>0', 'EXISTS(SELECT 1 FROM sources s WHERE s.sha=t.sha)']
    parameters = []
    if contains:
        where.append('casefold(t.text) LIKE ?')
        parameters.append('%' + contains.casefold() + '%')
    if max_length:
        where.append('t.length<=?')
        parameters.append(max_length)
    if publishable_only:
        where.append("EXISTS(SELECT 1 FROM sources s WHERE s.sha=t.sha AND s.chat_type='channel')")
    rows = db.execute('SELECT t.sha,t.text FROM texts t LEFT JOIN annotations a ON a.sha=t.sha WHERE '
                      + ' AND '.join(where) + ' ORDER BY t.sha LIMIT ?', (*parameters, size))
    return [{'sha': r['sha'], 'text': r['text']} for r in rows]


def record(db, path: Path, reviewed: bool = False):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    now = datetime.now(timezone.utc).isoformat()
    seen = set()
    with db:
        for row in rows:
            sha = row['sha']
            if sha in seen or not db.execute('SELECT 1 FROM texts WHERE sha=? AND length>0', (sha,)).fetchone():
                raise ValueError('Duplicate or unknown annotation')
            seen.add(sha)
            if row.get('provenance') != 'codex_agent' or row.get('version') != TOPICS['version']:
                raise ValueError('Annotation provenance/version missing')
            if set(row['labels']) != set(FEATURES) or any(v not in {'yes', 'no', 'unclear'} for v in row['labels'].values()):
                raise ValueError('Every feature needs an explicit yes/no/unclear label')
            if row['labels']['caption_has_context'] != 'yes' and any(row['labels'][t['id']] == 'yes' for t in TOPICS['topics']):
                raise ValueError('Insufficient caption cannot count as a positive topic example')
            required_reasons = {k for k, v in row['labels'].items() if v in {'yes', 'unclear'}}
            if any(not row.get('reasons', {}).get(k, '').strip() for k in required_reasons):
                raise ValueError('Positive and uncertain labels require an explanation')
            old = db.execute('SELECT labels_json FROM annotations WHERE sha=?', (sha,)).fetchone()
            if old and not reviewed:
                raise ValueError('Annotation already exists; explicit blind review required')
            encoded = json.dumps(row['labels'], ensure_ascii=False, sort_keys=True)
            db.execute('INSERT INTO annotation_history(sha,old_json,new_json,created_at,batch) VALUES (?,?,?,?,?)',
                       (sha, old['labels_json'] if old else None, encoded, now, path.name))
            db.execute('INSERT INTO annotations VALUES (?,?,?,?,?,?,?) ON CONFLICT(sha) DO UPDATE SET '
                       'labels_json=excluded.labels_json,reasons_json=excluded.reasons_json,created_at=excluded.created_at,'
                       'batch=excluded.batch,reviewed=excluded.reviewed',
                       (sha, TOPICS['version'], encoded, json.dumps(row['reasons'], ensure_ascii=False), now, path.name, int(reviewed)))
    return {'recorded': len(rows), **coverage(db)}


def record_compact(db, path: Path, reviewed: bool = False):
    """Разворачивает введённые агентом метки; текст при этом не анализируется.

    Формат: prefix | положительные JEBRPWVCTK | неясные | объяснение.
    Остальные метки агент явно задаёт как «нет» этой формой записи.
    """
    expanded = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        prefix, positive, uncertain, reason = [part.strip() for part in line.split('|', 3)]
        if len(prefix) < 8 or set(positive + uncertain) - set(ALIASES) or set(positive) & set(uncertain):
            raise ValueError('Invalid manually supplied labels')
        candidates = list(db.execute('SELECT sha FROM texts WHERE sha LIKE ?', (prefix + '%',)))
        if len(candidates) != 1:
            raise ValueError('Ambiguous annotation reference')
        labels = {feature: 'yes' if alias in positive else 'unclear' if alias in uncertain else 'no'
                  for alias, feature in ALIASES.items()}
        expanded.append(dict(sha=candidates[0]['sha'], version=TOPICS['version'], provenance='codex_agent',
                             labels=labels, reasons={k: reason for k, v in labels.items() if v != 'no'}))
    target = path.with_suffix('.jsonl')
    target.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in expanded), encoding='utf-8')
    return record(db, target, reviewed)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    cmd = commands.add_parser('import'); cmd.add_argument('snapshots', type=Path, nargs='+')
    cmd = commands.add_parser('next'); cmd.add_argument('--size', type=int, default=20)
    cmd.add_argument('--contains'); cmd.add_argument('--max-length', type=int)
    cmd.add_argument('--publishable-only', action='store_true')
    cmd = commands.add_parser('record'); cmd.add_argument('batch', type=Path); cmd.add_argument('--reviewed', action='store_true')
    cmd = commands.add_parser('record-compact'); cmd.add_argument('batch', type=Path); cmd.add_argument('--reviewed', action='store_true')
    commands.add_parser('coverage')
    args = parser.parse_args()
    db = connect(args.directory)
    try:
        if args.command == 'import':
            result = {'imported_rows': sum(import_snapshot(db, p) for p in args.snapshots), **coverage(db)}
        elif args.command == 'next':
            result = next_batch(db, args.size, args.contains, args.max_length, args.publishable_only)
        elif args.command == 'record':
            result = record(db, args.batch, args.reviewed)
        elif args.command == 'record-compact':
            result = record_compact(db, args.batch, args.reviewed)
        else:
            result = coverage(db)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        db.close()


if __name__ == '__main__':
    main()
