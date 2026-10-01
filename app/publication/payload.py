"""Снимок оригинала и разбиение без обрезки текста или потери вложений."""
from __future__ import annotations

import hashlib
import json
import mimetypes
from collections import Counter
from pathlib import Path

from PIL import Image
from app.taxonomy.artifact import file_sha256
from app.web.source_media import downloaded_media_path

TEXT_LIMIT = 4000


class PreparationError(ValueError):
    pass


def sha_json(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def text_units(text: str) -> int:
    # UTF-16 — консервативный предел для emoji, даже если сервер считает codepoints.
    return len(text.encode('utf-16-le')) // 2


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    if limit < 2:
        raise ValueError('Text limit must fit a Unicode character')
    chunks = []
    while text:
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if text_units(text[:middle]) <= limit:
                low = middle
            else:
                high = middle - 1
        end = low
        if end < len(text):
            separator = max(text.rfind('\n', 0, end), text.rfind(' ', 0, end))
            if separator >= end // 2:
                end = separator + 1
        chunks.append(text[:end])
        text = text[end:]
    return chunks or ['']


def media_manifest(post, media_dir: str) -> dict:
    path = downloaded_media_path(post, media_dir)
    if path is None:
        raise PreparationError(f'Медиа {post.message_id}: {post.media_download_status}')
    size = path.stat().st_size
    if size <= 0 or size > 104857600:
        raise PreparationError(f'Медиа {post.message_id}: размер вне разрешённого диапазона')
    mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    kind = 'file'
    if mime.startswith('image/') and path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.gif', '.tiff', '.tif', '.bmp', '.heic'}:
        kind = 'image'
        if size > 50 * 1024 * 1024:
            raise PreparationError(f'Изображение {post.message_id}: предел MAX 50 МБ')
        # Открываем только заголовок; не декодируем большую картинку целиком в RAM.
        if path.suffix.lower() != '.heic':
            try:
                with Image.open(path) as image:
                    if max(image.size) > 7680:
                        raise PreparationError(f'Изображение {post.message_id}: предел MAX 7680 пикселей')
            except OSError:
                raise PreparationError(f'Изображение {post.message_id}: файл не открывается') from None
    elif mime.startswith('video/') and path.suffix.lower() in {'.mp4', '.mov', '.mkv', '.webm'}:
        kind = 'video'
    elif mime.startswith('audio/'):
        kind = 'audio'
        media = (post.raw or {}).get('media') or {}
        document = media.get('document') or {}
        attributes = document.get('attributes') or []
        if any(float(a.get('duration') or 0) > 3600 for a in attributes):
            raise PreparationError(f'Аудио {post.message_id}: предел MAX 60 минут')
    return {'post_id': post.id, 'message_id': post.message_id, 'path': str(path),
            'name': path.name, 'size': size, 'mime': mime, 'type': kind, 'sha256': file_sha256(path)}


def source_fingerprint(posts):
    return sha_json([{'id': p.id, 'message_id': p.message_id, 'text': p.text,
        'deleted': p.is_deleted, 'entities': (p.raw or {}).get('entities') or [],
        'raw_media': (p.raw or {}).get('media'), 'media_path': p.media_path,
        'media_status': p.media_download_status}
        for p in sorted(posts, key=lambda p: p.message_id)])


def embedded_links(post):
    result = []
    text = post.text or ''
    for entity in (post.raw or {}).get('entities') or []:
        url = entity.get('url')
        if not isinstance(url, str) or not url.startswith(('https://', 'http://')) or url in text or url in result:
            continue
        # В Telegram «тут» может быть скрытой ссылкой. Сохраняем её отдельной
        # строкой: исходный текст остаётся неизменным, ссылка не исчезает.
        result.append(url)
    return result


def build_snapshot(posts, chat, source_url: str, media_dir: str) -> dict:
    if chat.folder_name != 'MAX' or chat.chat_type != 'channel':
        raise PreparationError('Отправляются только каналы из папки MAX, групповые разговоры исключены')
    ordered = sorted(posts, key=lambda p: p.message_id)
    if not ordered or any(p.is_deleted for p in ordered):
        raise PreparationError('Исходный пост удалён')
    captions = []
    for post in ordered:
        caption = post.text or ''
        if caption.strip() and caption not in captions:
            captions.append(caption)
    if not captions:
        raise PreparationError('Только медиа: требуется ручной разбор темы')
    original = '\n\n'.join(captions)
    links = list(dict.fromkeys(url for post in ordered for url in embedded_links(post)))
    text = original + ('\n\nСсылки из поста:\n' + '\n'.join(links) if links else '') + '\n\nИсточник: ' + source_url
    attachments = [media_manifest(post, media_dir) for post in ordered if post.media_type]
    key = f'{chat.peer_id}:album:{ordered[0].grouped_id}' if ordered[0].grouped_id else f'{chat.peer_id}:message:{ordered[0].message_id}'
    content = {'text': original, 'links': links, 'media': [{'sha256': m['sha256'], 'type': m['type']} for m in attachments]}
    return {'source_key': key, 'source_url': source_url, 'text': text,
            'original_text': original, 'media': attachments,
            'message_ids': [p.message_id for p in ordered], 'content_sha256': sha_json(content),
            'source_fingerprint': source_fingerprint(ordered)}


def message_parts(snapshot: dict) -> list[dict]:
    media_groups = []
    for item in snapshot['media']:
        if item['type'] in {'image', 'video'}:
            if media_groups and all(m['type'] in {'image', 'video'} for m in media_groups[-1]) and len(media_groups[-1]) < 12:
                media_groups[-1].append(item)
            else:
                media_groups.append([item])
        else:
            media_groups.append([item])
    texts = split_text(snapshot['text'])
    count = max(len(texts), len(media_groups), 1)
    return [{'text': texts[i] if i < len(texts) else '',
             'media': media_groups[i] if i < len(media_groups) else []}
            for i in range(count)]


def receipt_sha256(message: dict) -> str:
    """Отпечаток ответа MAX без изменяемых ссылок CDN и открытых токенов.

    Постоянные ID остаются только внутри хеша. Они позволяют сверить вложения
    ответа отправки с повторным чтением конкретного MID, а не только их число.
    """
    def stable(value):
        if isinstance(value, dict):
            # MAX выдаёт разные download-token при GET /messages и GET /messages/MID.
            # Проверено на реальном канале: photo_id одинаков, token меняется.
            return {k: stable(v) for k, v in value.items() if k not in {'url', 'urls', 'thumbnail', 'preview', 'token'}}
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value
    body = message.get('body') or {}
    return sha_json({'mid': body.get('mid'), 'text': body.get('text') or '',
                     'attachments': [stable(a) for a in body.get('attachments') or [] if a.get('type') != 'share']})


def uploaded_media_matches(uploads: list, message: dict) -> bool:
    actual = [a for a in (message.get('body') or {}).get('attachments') or [] if a.get('type') != 'share']
    if len(actual) != len(uploads):
        return False
    for upload, attachment in zip(uploads, actual, strict=True):
        identity = upload.get('_identity') or {}
        if not identity or upload['type'] != attachment.get('type'):
            return False
        if any(str((attachment.get('payload') or {}).get(key)) != str(value) for key, value in identity.items()):
            return False
    return True


def verify_message(message: dict, expected: dict, *, chat_id: int | None = None,
                   mid: str | None = None, receipt: str | None = None) -> bool:
    body = message.get('body') or {}
    actual = Counter(a.get('type') for a in (body.get('attachments') or []) if a.get('type') != 'share')
    required = Counter(m['type'] for m in expected['media'])
    return ((body.get('text') or '') == expected['text'] and actual == required
            and (mid is None or body.get('mid') == mid)
            and (chat_id is None or (message.get('recipient') or {}).get('chat_id') == chat_id)
            and (receipt is None or receipt_sha256(message) == receipt))
