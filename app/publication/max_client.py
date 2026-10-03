"""Один HTTP-клиент на sender; токен не попадает в URL, ошибки или web.

POST /messages после таймаута имеет неопределённый результат. Такой запрос
никогда не повторяем сами: сначала требуется сверка с опубликованным каналом.
"""
from __future__ import annotations

import json
import logging
import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.publication.payload import PreparationError, TEXT_LIMIT, text_units


class MaxApiError(Exception):
    def __init__(self, status: int | None, code: str, *, unknown: bool = False):
        # Не включаем response.text, URL загрузки или exception repr с секретами.
        self.status = status
        self.code = code if re.fullmatch(r'[\w.-]{1,100}', code) else 'api_error'
        self.unknown = unknown
        super().__init__(f'MAX: {status or "network"} / {self.code}')


def ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    directory = Path(__file__).resolve().parents[2] / 'config/certs'
    for certificate in sorted(directory.glob('russian_trusted*.crt')):
        context.load_verify_locations(cafile=str(certificate))
    return context


def approved_upload_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    hostname = (parsed.hostname or '').lower()
    return (parsed.scheme == 'https' and port in {None, 443}
            and parsed.username is None and parsed.password is None
            and any(hostname.endswith('.' + host) for host in ('oneme.ru', 'okcdn.ru')))


class MaxClient:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None):
        if not token.strip():
            raise ValueError('MAX token is missing')
        self.token = token.strip()
        self.bot_id = None
        self.http = client or httpx.AsyncClient(verify=ssl_context(), follow_redirects=False,
                                              timeout=httpx.Timeout(120, connect=10),
                                              limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
        # Даже информационный журнал httpx содержит URL upload с временным токеном.
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)

    async def aclose(self):
        await self.http.aclose()

    async def request(self, method: str, path: str, **kwargs) -> dict:
        sending = method == 'POST' and path == '/messages'
        try:
            response = await self.http.request(method, 'https://platform-api2.max.ru' + path,
                                               headers={'Authorization': self.token, 'Accept': 'application/json'}, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise MaxApiError(None, 'connect_failed') from None
        except httpx.TransportError:
            raise MaxApiError(None, 'transport_failed', unknown=sending) from None
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            raise MaxApiError(response.status_code, 'invalid_response', unknown=sending) from None
        if response.status_code >= 400:
            code = data.get('code', 'api_error') if isinstance(data, dict) else 'invalid_response'
            raise MaxApiError(response.status_code, str(code),
                              unknown=sending and response.status_code >= 500)
        if not isinstance(data, dict):
            raise MaxApiError(response.status_code, 'invalid_response', unknown=sending)
        return data

    async def check_channel(self, chat_id: int) -> dict:
        member = await self.request('GET', f'/chats/{chat_id}/members/me')
        permissions = member.get('permissions') or []
        if not member.get('is_admin') or not {'write', 'read_all_messages'}.issubset(permissions):
            raise MaxApiError(403, 'missing_write_or_read')
        chat = await self.request('GET', f'/chats/{chat_id}')
        return {'permissions': permissions, 'title': chat.get('title'), 'url': chat.get('link')}

    async def identity(self) -> int:
        if self.bot_id is None:
            me = await self.request('GET', '/me')
            if not isinstance(me.get('user_id'), int):
                raise MaxApiError(None, 'invalid_bot_identity')
            self.bot_id = me['user_id']
        return self.bot_id

    async def history(self, chat_id: int, before_ms: int | None = None) -> list[dict]:
        parameters = {'chat_id': chat_id, 'count': 100}
        if before_ms is not None:
            # В MAX from — верхняя граница времени, порядок выдачи обратный.
            parameters['from'] = before_ms
        result = await self.request('GET', '/messages', params=parameters)
        messages = result.get('messages')
        if not isinstance(messages, list):
            raise MaxApiError(None, 'invalid_history')
        return messages

    async def get_message(self, mid: str) -> dict:
        if not re.fullmatch(r'(?:mid\.)?[a-zA-Z0-9_-]+', mid):
            raise ValueError('Invalid MAX message ID')
        return await self.request('GET', f'/messages/{mid}')

    async def upload(self, media: dict) -> dict:
        path = Path(media['path'])
        from app.taxonomy.artifact import file_sha256
        if not path.is_file() or file_sha256(path) != media['sha256']:
            raise PreparationError('Вложение изменилось после подготовки')
        metadata = await self.request('POST', '/uploads', params={'type': media['type']})
        url = metadata.get('url', '')
        if not approved_upload_url(url):
            raise MaxApiError(None, 'untrusted_upload_host')
        try:
            with path.open('rb') as source:
                # Только image endpoint требует Authorization; не переносим
                # заголовок API автоматически на произвольную ссылку CDN.
                headers = {'Authorization': self.token} if media['type'] == 'image' else {}
                response = await self.http.post(url, headers=headers,
                                                files={'data': (media['name'], source, media['mime'])})
        except httpx.TransportError:
            raise MaxApiError(None, 'upload_transport_failed') from None
        if response.status_code != 200:
            raise MaxApiError(response.status_code, 'upload_failed')
        token = metadata.get('token')
        identity = {}
        if media['type'] in {'image', 'file'}:
            try:
                uploaded = response.json()
            except ValueError:
                raise MaxApiError(None, 'invalid_upload_response') from None
            if not isinstance(uploaded, dict):
                raise MaxApiError(None, 'invalid_upload_response')
            if media['type'] == 'image':
                photos = list((uploaded.get('photos') or {}).values())
                if len(photos) != 1 or not isinstance(photos[0], dict):
                    raise MaxApiError(None, 'ambiguous_image_upload')
                token = photos[0].get('token')
                identity = {'photo_id': next(iter(uploaded['photos']))}
            else:
                token = uploaded.get('token')
        elif response.text.strip() != '<retval>1</retval>':
            raise MaxApiError(None, 'incomplete_media_upload')
        if not isinstance(token, str) or not token:
            raise MaxApiError(None, 'missing_media_token')
        return {'type': media['type'], 'payload': {'token': token}, '_identity': identity}

    async def send(self, chat_id: int, text: str, attachments: list, reply_mid: str | None = None,
                   *, source_url: str | None = None) -> dict:
        from app.publication.payload import formatted_text
        wire_text = formatted_text(text, source_url) if source_url else text
        if text_units(wire_text) > TEXT_LIMIT:
            raise PreparationError('Текст не был разделён на допустимые части')
        body = {'text': wire_text or None, 'attachments': [{'type': a['type'], 'payload': a['payload']} for a in attachments], 'notify': True}
        if source_url:
            body['format'] = 'html'
        if reply_mid:
            body['link'] = {'type': 'reply', 'mid': reply_mid}
        data = await self.request('POST', '/messages', params={'chat_id': chat_id, 'disable_link_preview': True}, json=body)
        message = data.get('message') or {}
        if not isinstance(message, dict) or not (message.get('body') or {}).get('mid'):
            raise MaxApiError(None, 'missing_message_id', unknown=True)
        return message
