"""Read MAX texts from quiet chats into a private training-only snapshot.

This does not write to PostgreSQL, download media, or alter the site. It runs
only after the normal collector has stopped, so the same Telethon session is
never opened by two processes. Raw text is captured directly into a protected
local file; ordinary stdout contains aggregate counts only.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


REMOTE_PROGRAM = r'''
import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.folders import resolve_folder_chats
from app.telegram_client import create_telegram_client

EXCLUDED = set(__EXCLUDED__)
SELECTED = set(__SELECTED__)
PER_CHAT_LIMIT = __PER_CHAT_LIMIT__
LOOKBACK_DAYS = __LOOKBACK_DAYS__

async def main():
    settings = Settings()
    client = create_telegram_client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError('Telegram session is not authorized')
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=LOOKBACK_DAYS)
        chats = await resolve_folder_chats(client, 'MAX')
        for chat in chats:
            if chat.peer_id in EXCLUDED or (SELECTED and chat.peer_id not in SELECTED):
                continue
            seen = 0
            async for message in client.iter_messages(chat.entity, limit=PER_CHAT_LIMIT):
                date = message.date
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                if date.astimezone(timezone.utc) < cutoff:
                    break
                print(json.dumps({
                    'chat_peer_id': chat.peer_id,
                    'message_id': message.id,
                    'date': date.isoformat(),
                    'text': message.raw_text or '',
                    'has_media': bool(message.media),
                }, ensure_ascii=False), flush=True)
                seen += 1
                if seen >= PER_CHAT_LIMIT:
                    break
    finally:
        await client.disconnect()

asyncio.run(main())
'''


def read_jsonl_gzip(path: Path):
    with gzip.open(path, 'rt', encoding='utf-8') as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def export(snapshot: Path, private_directory: Path, quiet_max: int, per_chat: int, days: int, host: str, selected_chats: list[int]):
    if private_directory.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError('Output must be outside the Git checkout')
    private_directory.mkdir(parents=True, exist_ok=True)
    counts = collections.Counter()
    known: set[tuple[int, int]] = set()
    for row in read_jsonl_gzip(snapshot):
        peer_id = int(row['chat_peer_id'])
        counts[peer_id] += 1
        known.add((peer_id, int(row['message_id'])))
    if not counts:
        raise ValueError('Empty initial snapshot')

    status = subprocess.run(
        ['ssh', host, "sudo docker inspect -f '{{.State.Status}}' portfolio-publisher-taxonomy-sync"],
        capture_output=True, text=True, check=False,
    )
    if status.returncode == 0 and status.stdout.strip() == 'running':
        raise SystemExit('Normal MAX sync is still running; avoid a second Telethon session')

    # Chats absent from the weekly snapshot have zero weekly posts and stay
    # eligible because the remote program resolves the live MAX folder.
    excluded = [] if selected_chats else [peer for peer, count in counts.items() if count > quiet_max]
    program = REMOTE_PROGRAM.replace('__EXCLUDED__', json.dumps(excluded))
    program = program.replace('__SELECTED__', json.dumps(selected_chats))
    program = program.replace('__PER_CHAT_LIMIT__', str(per_chat)).replace('__LOOKBACK_DAYS__', str(days))
    command = [
        'ssh', host,
        'cd /srv/portfolio/publisher && sudo docker compose --env-file .env '
        '-f compose.yaml run --rm -T --no-deps collector python -',
    ]
    result = subprocess.run(command, input=program.encode('utf-8'), capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f'Telegram supplement failed (exit {result.returncode})')

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = private_directory / f'max-supplement-{timestamp}.jsonl.gz'
    temporary = private_directory / f'.{target.name}.tmp'
    digest = hashlib.sha256()
    count = 0
    ids: set[int] = set()
    chats: set[int] = set()
    try:
        with gzip.open(temporary, 'wb') as output:
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                key = (int(row['chat_peer_id']), int(row['message_id']))
                if key in known or not row.get('text', '').strip():
                    continue
                known.add(key)
                # Stable negative IDs allow several independent supplemental
                # pulls to be merged without colliding with PostgreSQL IDs.
                stable_id = -((int.from_bytes(hashlib.sha256(f'{key[0]}:{key[1]}'.encode()).digest()[:8], 'big') & ((1 << 62) - 1)) + 10_000_000)
                if stable_id in ids:
                    raise ValueError('training-only ID collision')
                ids.add(stable_id)
                record = {
                    'id': stable_id,
                    **row,
                    'media_type': 'attached' if row['has_media'] else None,
                    'grouped_id': None,
                    'is_deleted': False,
                    'source': 'telegram_supplement_training_only',
                }
                encoded = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
                output.write(encoded)
                digest.update(encoded)
                chats.add(key[0])
                count += 1
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    manifest = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source': 'Telegram MAX, recent texts from quiet chats, training only',
        'file': target.name,
        'new_text_posts': count,
        'chats_with_new_text': len(chats),
        'quiet_weekly_posts_max': quiet_max,
        'selection': 'explicit_chat_ids' if selected_chats else 'quiet_chats',
        'selected_chat_count': len(selected_chats),
        'per_chat_recent_limit': per_chat,
        'lookback_days': days,
        'jsonl_sha256': digest.hexdigest(),
        'limitations': 'Recent messages only, at most the configured per-chat limit; no media content was analyzed.',
    }
    target.with_suffix('.manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('private_directory', type=Path)
    parser.add_argument('--quiet-max', type=int, default=30)
    parser.add_argument('--per-chat', type=int, default=100)
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--selected-chat-id', type=int, action='append', default=[])
    parser.add_argument('--host', default='wtg-prod-vdsina')
    args = parser.parse_args()
    if min(args.quiet_max, args.per_chat, args.days) <= 0:
        parser.error('Limits must be positive')
    print(json.dumps(export(args.snapshot, args.private_directory, args.quiet_max, args.per_chat, args.days, args.host, args.selected_chat_id)))


if __name__ == '__main__':
    main()
