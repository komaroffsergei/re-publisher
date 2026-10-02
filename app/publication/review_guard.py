"""Узкий стоп отправки. Тематическая оценка не является разрешением публикации.

Прочитанные Codex исключения приходят по хешам из защищённой настройки
маршрута. Здесь нет разметки корпуса, OCR, запросов к LLM или запуска команд.
Срабатывание на чувствительную формулировку означает ручной разбор, а не
утверждение о намерениях автора. Исходник и оценки остаются в publisher.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlsplit

from app.publication.payload import PreparationError, sha_json

GUARD_VERSION = 'max-publication-review-20261002-v1'
_MINOR = re.compile(r'школьни[кц]|подрост[ко]|несовершеннолет|дет(?:и|ей|ск)|реб[её]н(?:ок|ка)|(?:1[0-7])\s*[-–]?\s*(?:лет|летн|year.old)|\b(?:child|children|teenage|underage|schoolgirl|schoolboy)\b', re.I)
_SEXUAL = re.compile(r'порн|эротич|эротик|сексуаль|обнаж[её]|раздет|\bгол(?:ая|ые|ый|ую|ых|ого)\b|\b(?:porn|nsfw|nude|naked|sexual|sex)\b', re.I)
_UNDRESS = re.compile(r'\b(?:nudify|undress)\b|раздеватор|(?:раздеть|раздева[ею])[^.!?\n]{0,90}(?:фот|девуш|женщ|школь|челов)', re.I)
_TOKEN_KEYS = {'api_key', 'apikey', 'access_token', 'token', 'auth_token', 'secret', 'private_key'}


class PublicationHold(PreparationError):
    def __init__(self, reason: str, snapshot: dict):
        super().__init__(reason)
        self.snapshot = snapshot


def caption_sha(text: str) -> str:
    return hashlib.sha256(re.sub(r'\s+', ' ', text or '').strip().encode()).hexdigest()


def policy_error(policy) -> str | None:
    if not isinstance(policy, dict) or policy.get('version') != GUARD_VERSION:
        return 'Не закреплена проверка содержимого перед отправкой'
    holds = policy.get('holds')
    if (not isinstance(holds, dict) or len(holds) > 5000
            or any(not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key) or not isinstance(value, str)
                   or not 1 <= len(value) <= 300 for key, value in holds.items())
            or policy.get('sha256') != sha_json(holds)):
        return 'Настройка отложенных постов повреждена'
    return None


def hold_reason(snapshot: dict, policy: dict) -> str | None:
    if error := policy_error(policy):
        return error
    original = snapshot['original_text']
    # Проверяем и весь альбом, и его подписи по отдельности.
    captions = snapshot.get('original_captions') or [original]
    for caption in [original, *captions]:
        if reason := policy['holds'].get(caption_sha(caption)):
            return 'Отложено после просмотра Codex: ' + reason
    if _MINOR.search(original) and _SEXUAL.search(original):
        return 'Нужен ручной разбор: несовершеннолетние и сексуальный контекст'
    if _UNDRESS.search(original):
        return 'Нужен ручной разбор: раздевание человека на фото или видео'
    # Секрет не повторяем в сообщении ошибки. «YOUR_TOKEN» в учебном примере
    # не равен реальному ключу, но открытый длинный токен в URL не пересылаем.
    urls = re.findall(r'https?://[^\s<>]+', snapshot['text'])
    for url in urls:
        try:
            query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        except ValueError:
            continue
        for key, value in query:
            if (key.casefold() in _TOKEN_KEYS and len(value) >= 20
                    and not re.search(r'your[_-]|example|placeholder|change[_-]?me', value, re.I)):
                return 'Нужен ручной разбор: ссылка содержит похожее на ключ доступа значение'
    return None
