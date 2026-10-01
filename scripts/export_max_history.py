"""Возобновляемая выгрузка MAX только для локального учебного корпуса.

Не пишет в рабочую БД и не скачивает вложения. На время короткой партии
останавливает collector, затем обязательно возвращает его прежнее состояние.
Тексты идут по SSH прямо в защищённый файл, в терминал выводятся только числа.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


REMOTE_PROGRAM = r'''
import asyncio, json, logging, time
from telethon.errors import FloodWaitError
from app.config import Settings
from app.folders import resolve_folder_chats
from app.telegram_client import create_telegram_client

OPTIONS = __OPTIONS__
logging.getLogger('telethon').setLevel(logging.ERROR)

def emit(row):
    print(json.dumps(row, ensure_ascii=False, separators=(',', ':')), flush=True)

async def main():
    client = create_telegram_client(Settings())
    client.flood_sleep_threshold = 0
    started = time.monotonic()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError('Session is not authorized')
        chats = await resolve_folder_chats(client, 'MAX')
        for chat in chats:
            emit(dict(kind='chat', chat_peer_id=chat.peer_id, title=chat.title,
                      username=chat.username, chat_type=chat.chat_type,
                      folder_name='MAX'))
        states = OPTIONS['states']
        remaining = [c for c in chats if not states.get(str(c.peer_id), {}).get('exhausted')]
        # Сначала ещё не открывавшиеся чаты: большие группы не вытесняют каналы.
        remaining.sort(key=lambda c: (states.get(str(c.peer_id), {}).get('pages', 0), c.peer_id))
        for chat in remaining:
            if time.monotonic() - started >= OPTIONS['seconds']:
                break
            state = states.get(str(chat.peer_id), {})
            offset = int(state.get('offset_id', 0))
            count = 0
            complete = True
            try:
                iterator = client.iter_messages(chat.entity, limit=OPTIONS['page_size'], offset_id=offset)
                async for message in iterator:
                    emit(dict(kind='post', chat_peer_id=chat.peer_id,
                              message_id=message.id, date=message.date.isoformat() if message.date else None,
                              text=message.raw_text or '', has_media=bool(message.media),
                              grouped_id=getattr(message, 'grouped_id', None),
                              media_type=type(message.media).__name__ if message.media else None,
                              chat_type=chat.chat_type, folder_name='MAX'))
                    count += 1
                    offset = message.id
                    if time.monotonic() - started >= OPTIONS['seconds']:
                        complete = False
                        break
                emit(dict(kind='progress', chat_peer_id=chat.peer_id, offset_id=offset,
                          count=count, exhausted=complete and count < OPTIONS['page_size']))
            except FloodWaitError as exc:
                emit(dict(kind='progress', chat_peer_id=chat.peer_id, offset_id=offset,
                          count=count, exhausted=False, error='FloodWait', retry_after_seconds=exc.seconds))
                break
            except Exception as exc:
                emit(dict(kind='progress', chat_peer_id=chat.peer_id, offset_id=offset,
                          count=count, exhausted=False, error=type(exc).__name__))
        emit(dict(kind='finished', seconds=round(time.monotonic()-started, 2)))
    finally:
        await client.disconnect()

asyncio.run(asyncio.wait_for(main(), timeout=OPTIONS['seconds'] + 15))
'''

# Один владелец файла Telethon-сессии. EXIT срабатывает и при ошибке Python;
# отдельное восстановление ниже страхует обрыв SSH. Никакие секреты не печатаются.
REMOTE_COMMAND = r'''
set -eu
cd /srv/portfolio/publisher
install -d -m 700 /run/publisher-training
exec 9>/run/publisher-training/export.lock
flock -n 9 || exit 75
running=$(docker inspect -f '{{.State.Running}}' portfolio-publisher-collector-1)
restore() {
    docker rm -f portfolio-publisher-training-export >/dev/null 2>&1 || true
    if [ "$running" = true ]; then
        docker compose --env-file .env -f compose.yaml start collector >/dev/null
    fi
}
trap restore EXIT
trap 'exit 130' HUP INT TERM
docker compose --env-file .env -f compose.yaml stop -t 15 collector >/dev/null
docker compose --env-file .env -f compose.yaml run --name portfolio-publisher-training-export --rm -T --no-deps collector python -
'''


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def training_id(peer: int, message: int) -> int:
    value = hashlib.sha256(f'{peer}:{message}'.encode()).digest()
    return -((int.from_bytes(value[:8], 'big') & ((1 << 62) - 1)) + 10_000_000)


def ensure_private(path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    resolved = path.resolve()
    if resolved.is_relative_to(root) or '.git' in resolved.parts:
        raise ValueError('Corpus must remain outside the checkout')
    path.mkdir(parents=True, exist_ok=True)


def status(host: str) -> dict:
    command = "sudo docker inspect -f '{{json .State}}' portfolio-publisher-collector-1"
    result = subprocess.run(['ssh', host, command], capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Collector status unavailable')
    state = json.loads(result.stdout)
    return {'running': state['Running'], 'health': state.get('Health', {}).get('Status')}


def export_batch(directory: Path, host: str, page_size: int, seconds: int) -> dict:
    ensure_private(directory)
    progress_file = directory / 'history-progress.json'
    progress = json.loads(progress_file.read_text(encoding='utf-8')) if progress_file.exists() else {'states': {}, 'chats': {}, 'batches': []}
    # FloodWait относится к аккаунту, а не к очередному запуску скрипта.
    # Пока он действует, collector не останавливаем и новую выгрузку не начинаем.
    blocked_until = progress.get('blocked_until')
    if blocked_until and datetime.fromisoformat(blocked_until) > datetime.now(timezone.utc):
        return {'backoff_until': blocked_until, 'posts': 0, 'collector_running': status(host)['running']}
    before = status(host)
    if not before['running']:
        raise RuntimeError('Collector is not running; do not change its state implicitly')
    options = dict(states=progress['states'], page_size=page_size, seconds=seconds)
    program = REMOTE_PROGRAM.replace('__OPTIONS__', repr(options))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = directory / f'history-{stamp}.jsonl.gz'
    temporary = target.with_suffix(target.suffix + '.partial')
    errors = directory / f'history-{stamp}.stderr.private'
    count = 0
    digest = hashlib.sha256()
    updates = {}
    finished = False
    process = None
    try:
        with errors.open('wb') as stderr, gzip.open(temporary, 'wb') as output:
            process = subprocess.Popen(['ssh', host, 'sudo bash -c ' + shlex.quote(REMOTE_COMMAND)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=stderr)
            process.stdin.write(program.encode('utf-8'))
            process.stdin.close()
            for line in process.stdout:
                row = json.loads(line)
                kind = row.pop('kind')
                peer = str(row.get('chat_peer_id', ''))
                if kind == 'post':
                    row['id'] = training_id(int(peer), row['message_id'])
                    row['source'] = 'telegram_max_training_history'
                    row['is_deleted'] = False
                    encoded = json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
                    output.write(encoded)
                    digest.update(encoded)
                    count += 1
                elif kind == 'chat':
                    progress['chats'][peer] = row
                elif kind == 'progress':
                    previous = progress['states'].get(peer, {})
                    updates[peer] = {**row, 'pages': previous.get('pages', 0) + 1,
                                     'exported': previous.get('exported', 0) + row['count']}
                    if row.get('error') == 'FloodWait':
                        progress['blocked_until'] = (datetime.now(timezone.utc)
                            + timedelta(seconds=max(0, int(row['retry_after_seconds'])))).isoformat()
                elif kind == 'finished':
                    finished = True
            code = process.wait(timeout=30)
        # Незавершённая партия тоже сохраняется: её тексты не теряются.
        # Курсор закрытых чатов обновляем только после атомарного сохранения файла.
        temporary.replace(target)
        progress['states'].update(updates)
        entry = dict(file=target.name, posts=count, jsonl_sha256=digest.hexdigest(),
                     finished=finished, exit_code=code, closed_chats=len(updates), created_at=stamp)
        progress['batches'].append(entry)
        atomic_json(progress_file, progress)
        atomic_json(target.with_suffix('.manifest.json'), entry)
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=20)
        # При любой ошибке обеспечиваем возобновление realtime и сверки.
        # Захватываем тот же lock. При занятом lock не убиваем чужую выгрузку
        # и не открываем её Telethon-session параллельно с collector.
        cleanup = "set -eu\nexec 9>/run/publisher-training/export.lock\nflock -n 9 || exit 75\ncd /srv/portfolio/publisher\ndocker rm -f portfolio-publisher-training-export >/dev/null 2>&1 || true\ndocker compose --env-file .env -f compose.yaml start collector"
        restored = subprocess.run(['ssh', host, 'sudo bash -c ' + shlex.quote(cleanup)],
                                  capture_output=True, timeout=30)
        if restored.returncode not in {0, 75}:
            raise RuntimeError('Collector restart failed; manual recovery required')
    after = status(host)
    if not after['running']:
        raise RuntimeError('Collector did not restart')
    if code or not finished:
        raise RuntimeError(f'Export incomplete, exit {code}; private diagnostic: {errors.name}')
    return {**entry, 'folder_chats': len(progress['chats']),
            'exhausted_chats': sum(s.get('exhausted', False) for s in progress['states'].values()),
            'collector_running': after['running'],
            'errors': {k: v['error'] for k, v in updates.items() if v.get('error')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--host', default='wtg-prod-vdsina')
    parser.add_argument('--page-size', type=int, default=200, choices=range(10, 501))
    parser.add_argument('--seconds', type=int, default=40, choices=range(5, 51))
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(export_batch(args.directory, args.host, args.page_size, args.seconds)))


if __name__ == '__main__':
    main()
