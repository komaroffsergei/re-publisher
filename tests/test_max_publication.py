from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.publication.max_client import MaxApiError, MaxClient, approved_upload_url
from app.publication.payload import (PreparationError, build_snapshot, message_parts, receipt_sha256,
                                     split_text, text_units, uploaded_media_matches, verify_message, sha_json, formatted_text,
                                     original_matches_snapshot)
from app.publication.service import gate_error
from app.publication.review_guard import GUARD_VERSION
from app.web.publication_routes import require_initial_manifest
from app.taxonomy.artifact import artifact_version, checkpoint_path, file_sha256


@pytest.mark.parametrize('text', ['', 'a' * 8100, ('Пост🙂\n  следующий абзац\n' * 700), '🙂' * 4100, '  original\n\n '], ids=['empty', 'long-ascii', 'paragraphs', 'emoji', 'whitespace'])
def test_text_is_losslessly_split(text):
    chunks = split_text(text)
    assert ''.join(chunks) == text
    assert all(text_units(c) <= 4000 for c in chunks)


def test_media_order_and_platform_compatibility():
    media = [{'type': kind, 'id': i} for i, kind in enumerate(['image'] * 13 + ['file', 'image', 'audio', 'video', 'file'])]
    parts = message_parts({'text': 'caption', 'media': media})
    assert [m for p in parts for m in p['media']] == media
    for part in parts:
        assert len(part['media']) <= 12
        if any(m['type'] in {'audio', 'file'} for m in part['media']):
            assert len(part['media']) == 1


def test_groups_cannot_be_published_even_when_text_matches():
    chat = SimpleNamespace(folder_name='MAX', chat_type='group')
    with pytest.raises(PreparationError, match='групповые'):
        build_snapshot([], chat, 'https://t.me/c/1/2', '.')


def test_original_whitespace_is_not_removed():
    post = SimpleNamespace(id=1, message_id=2, text='  original\n\n ', media_type=None, is_deleted=False, grouped_id=None, raw={}, media_path=None, media_download_status=None)
    chat = SimpleNamespace(peer_id=-1001, folder_name='MAX', chat_type='channel')
    snap = build_snapshot([post], chat, 'https://t.me/c/1/2', '.')
    assert snap['original_text'] == post.text
    assert snap['text'] == post.text + '\n\nИсточник'


def test_hidden_telegram_links_are_not_appended_to_original_caption():
    post = SimpleNamespace(id=1, message_id=2, text='Скачать тут', media_type=None, is_deleted=False,
        grouped_id=None, raw={'entities': [{'url': 'https://example.com/source', 'offset': 8, 'length': 3}]},
        media_path=None, media_download_status=None)
    chat = SimpleNamespace(peer_id=-1001, folder_name='MAX', chat_type='channel')
    snap = build_snapshot([post], chat, 'https://t.me/c/1/2', '.')
    assert snap['original_text'] == post.text
    assert 'https://example.com/source' not in snap['text']
    assert 'Ссылки из поста' not in snap['text']
    assert message_parts(snap)[0]['source_url'] == 'https://t.me/c/1/2'


def test_empty_and_deleted_album_are_not_silently_published():
    chat = SimpleNamespace(peer_id=-1001, folder_name='MAX', chat_type='channel')
    post = SimpleNamespace(id=1, message_id=2, text=' ', media_type=None, is_deleted=False, grouped_id=3)
    with pytest.raises(PreparationError, match='Только медиа'):
        build_snapshot([post], chat, 'https://t.me/c/1/2', '.')
    post.is_deleted = True
    with pytest.raises(PreparationError, match='удалён'):
        build_snapshot([post], chat, 'https://t.me/c/1/2', '.')


def test_ocr_media_only_keeps_original_attachment_and_source_without_transcript(tmp_path):
    from PIL import Image
    image = tmp_path / 'meme.jpg'
    Image.new('RGB', (20, 20)).save(image)
    post = SimpleNamespace(id=1, message_id=2, text='', media_type='MessageMediaPhoto',
        is_deleted=False, grouped_id=None, raw={}, media_path=str(image), media_download_status='downloaded')
    chat = SimpleNamespace(peer_id=-1001, folder_name='MAX', chat_type='channel')
    with pytest.raises(PreparationError, match='Только медиа'):
        build_snapshot([post], chat, 'https://t.me/c/1/2', str(tmp_path))
    snap = build_snapshot([post], chat, 'https://t.me/c/1/2', str(tmp_path), allow_ocr_media_only=True)
    assert snap['original_text'] == '' and snap['original_captions'] == []
    assert snap['text'] == 'Источник'
    assert snap['media'][0]['path'] == str(image)
    assert message_parts(snap)[0]['media'] == snap['media']
    image.unlink()
    with pytest.raises(PreparationError, match='Медиа'):
        build_snapshot([post], chat, 'https://t.me/c/1/2', str(tmp_path), allow_ocr_media_only=True)


def test_ocr_permission_does_not_make_empty_post_publishable():
    chat = SimpleNamespace(peer_id=-1001, folder_name='MAX', chat_type='channel')
    post = SimpleNamespace(id=1, message_id=2, text='', media_type=None, is_deleted=False, grouped_id=None)
    with pytest.raises(PreparationError, match='Только медиа'):
        build_snapshot([post], chat, 'https://t.me/c/1/2', '.', allow_ocr_media_only=True)


def test_verification_uses_stable_media_ids_not_rotating_tokens():
    message = {'body': {'mid': 'mid.1', 'text': 'source', 'attachments': [
        {'type': 'image', 'payload': {'photo_id': 99, 'token': 'temporary', 'url': 'https://cdn.example/temporary'}}]},
        'recipient': {'chat_id': -1}}
    expected = {'text': 'source', 'media': [{'type': 'image'}]}
    receipt = receipt_sha256(message)
    loaded = deepcopy(message)
    loaded['body']['attachments'][0]['payload'].update(token='different', url='https://cdn.example/different')
    assert verify_message(loaded, expected, mid='mid.1', chat_id=-1, receipt=receipt)
    loaded['body']['attachments'][0]['payload']['photo_id'] = 100
    assert not verify_message(loaded, expected, mid='mid.1', chat_id=-1, receipt=receipt)
    assert not verify_message(message, expected, mid='mid.2')
    assert not verify_message(message, expected, chat_id=-2)


def test_unknown_media_needs_matching_upload_id():
    message = {'body': {'attachments': [{'type': 'image', 'payload': {'photo_id': 99}}]}}
    uploads = [{'type': 'image', 'payload': {'token': 'private'}, '_identity': {'photo_id': '99'}}]
    assert uploaded_media_matches(uploads, message)
    uploads[0]['_identity'] = {}
    assert not uploaded_media_matches(uploads, message)


@pytest.mark.parametrize('url', ['http://iu.oneme.ru/u', 'https://oneme.ru.evil.test/u', 'https://evil.test/u', 'https://user@iu.oneme.ru/u', 'https://iu.oneme.ru:8443/u'])
def test_upload_host_is_constrained(url):
    assert not approved_upload_url(url)
    assert approved_upload_url('https://iu.oneme.ru/uploadImage?temporary=secret')


def valid_gate():
    version = SimpleNamespace(id=1, model_key='tfidf', expression={'x': 1})
    route = SimpleNamespace(approved_version_id=1, quality_gate={
        'model_key': 'tfidf', 'expression_sha256': sha_json(version.expression),
        'test_matched': 50, 'test_correct': 45, 'train_positive': 1000,
        'model_version': 'actual', 'test_sha256': 'a' * 64, 'split_sha256': 'b' * 64,
        'review_policy': {'version': GUARD_VERSION, 'holds': {}, 'sha256': sha_json({})}})
    return route, version


@pytest.mark.parametrize('key,value', [('test_matched', 49), ('test_correct', 44), ('train_positive', 999), ('train_positive', '1000'), ('test_matched', True), ('test_sha256', 'missing'), ('model_version', '')])
def test_route_requires_real_quota_and_heldout_gate(key, value):
    route, version = valid_gate()
    assert gate_error(route, version) is None
    route.quality_gate[key] = value
    assert gate_error(route, version)


def test_filter_change_invalidates_quality_gate():
    route, version = valid_gate()
    version.expression = {'x': 2}
    assert gate_error(route, version)


def test_owner_acceptance_does_not_fabricate_quality_or_allow_changed_filter():
    route, version = valid_gate()
    route.quality_gate['test_correct'] = 40
    route.quality_gate['owner_acceptance'] = {
        'version_id': version.id, 'accepted_at': '2026-10-03T12:00:00+00:00',
        'reason': 'Владелец разрешил экспериментальный маршрут, качество пока не прошло допуск.'}
    assert gate_error(route, version) is None
    assert route.quality_gate['test_correct'] == 40
    version.id = 2
    assert gate_error(route, version)


@pytest.mark.parametrize('value', [False, {}, {'version_id': 1, 'accepted_at': 'bad'},
    {'version_id': 1, 'accepted_at': '2026-10-03T12:00:00', 'reason': 'Согласован экспериментальный запуск'},
    {'version_id': 1, 'accepted_at': '2026-10-03T12:00:00+00:00', 'reason': 'short'}])
def test_invalid_owner_acceptance_is_rejected(value):
    route, version = valid_gate()
    route.quality_gate['owner_acceptance'] = value
    assert gate_error(route, version)


def test_owner_acceptance_keeps_content_review_policy_required():
    route, version = valid_gate()
    route.quality_gate['owner_acceptance'] = {'version_id': 1,
        'accepted_at': '2026-10-03T12:00:00+00:00', 'reason': 'Владелец разрешил экспериментальный запуск'}
    route.quality_gate['review_policy'] = {}
    assert gate_error(route, version)


def test_initial_batch_is_exactly_180_unique_new_posts():
    rows = [{'channel_id': ch, 'source_key': f'p-{i}', 'content_sha256': f'h-{i}', 'reviewed': True} for ch in range(9) for i in range(20)]
    require_initial_manifest({'items': rows})
    with pytest.raises(PreparationError):
        require_initial_manifest({'items': rows[:-1]})
    rows[0]['reviewed'] = False
    with pytest.raises(PreparationError):
        require_initial_manifest({'items': rows})


def test_loaded_model_version_and_checkpoint_are_from_artifact(tmp_path):
    weights = tmp_path / 'best.safetensors'; weights.write_bytes(b'actual weights')
    assert artifact_version(tmp_path, 'minilm', weights).startswith('minilm@')
    (tmp_path / 'model-manifest.json').write_text(json.dumps({'models': {'minilm': {'weights_sha256': file_sha256(weights), 'version': 'v3'}}}))
    assert artifact_version(tmp_path, 'minilm', weights) == 'v3'
    weights.write_bytes(b'different weights')
    with pytest.raises(ValueError): artifact_version(tmp_path, 'minilm', weights)
    with pytest.raises(ValueError): checkpoint_path(tmp_path, {'best_checkpoint': '../outside.safetensors'})


def test_calibration_or_tokenizer_change_invalidates_artifact(tmp_path):
    weights = tmp_path / 'best.safetensors'; weights.write_bytes(b'weights')
    config = tmp_path / 'training.json'; config.write_bytes(b'calibration')
    (tmp_path / 'model-manifest.json').write_text(json.dumps({'models': {'minilm': {
        'weights_sha256': file_sha256(weights), 'version': 'qa-version',
        'auxiliary_sha256': {'training.json': file_sha256(config)}}}}))
    assert artifact_version(tmp_path, 'minilm', weights) == 'qa-version'
    config.write_bytes(b'changed calibration')
    with pytest.raises(ValueError, match='configuration does not match'): artifact_version(tmp_path, 'minilm', weights)


async def test_send_timeout_is_unknown_and_token_does_not_leak():
    def handler(request):
        assert request.headers['authorization'] == 'private-token'
        raise httpx.ReadTimeout('upstream', request=request)
    client = MaxClient('private-token', client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(MaxApiError) as caught:
            await client.send(-1, 'original', [])
        assert caught.value.unknown
        assert 'private-token' not in str(caught.value)
    finally: await client.aclose()


async def test_confirmed_api_rejection_is_not_unknown():
    client = MaxClient('private-token', client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={'code': 'attachment.not.ready'}))))
    try:
        with pytest.raises(MaxApiError) as caught: await client.send(-1, 'original', [])
        assert not caught.value.unknown
        assert caught.value.code == 'attachment.not.ready'
    finally: await client.aclose()


async def test_http_500_or_invalid_response_cannot_be_blindly_retried():
    for response in [httpx.Response(500, json=['unavailable']), httpx.Response(200, text='not-json')]:
        client = MaxClient('secret', client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)))
        try:
            with pytest.raises(MaxApiError) as caught: await client.send(-1, 'original', [])
            assert caught.value.unknown
        finally: await client.aclose()


async def test_mid_prefix_is_valid_and_internal_upload_metadata_is_not_sent():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'message': {'body': {'mid': 'mid.abc'}}})
    client = MaxClient('secret', client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await client.get_message('mid.abc')
        await client.send(-1, 'original', [{'type': 'image', 'payload': {'token': 'upload'}, '_identity': {'photo_id': 1}}])
        body = json.loads(seen[-1].content)
        assert body['attachments'] == [{'type': 'image', 'payload': {'token': 'upload'}}]
        assert body['notify'] is True
    finally: await client.aclose()


def test_source_is_one_small_link_and_original_html_is_literal():
    text = '<b>Автор & ссылка https://example.com</b>🙂\n\nИсточник'
    wire = formatted_text(text, 'https://t.me/example/42')
    assert wire == ('&lt;b&gt;Автор &amp; ссылка https://example.com&lt;/b&gt;🙂\n\n'
                    '<i><a href="https://t.me/example/42">Источник</a></i>')
    with pytest.raises(PreparationError):
        formatted_text(text, 'https://evil.test/42')
    with pytest.raises(PreparationError):
        formatted_text('нет подписи', 'https://t.me/example/42')


def test_long_original_is_lossless_and_source_never_splits():
    original = ('Текст🙂 <>&\n' * 1000)
    parts = message_parts({'text': original + '\n\nИсточник', 'original_text': original,
                          'source_url': 'https://t.me/example/42', 'media': []})
    assert ''.join(p['text'] for p in parts) == original + '\n\nИсточник'
    assert sum('source_url' in p for p in parts) == 1
    assert 'source_url' in parts[-1]
    for p in parts:
        wire = formatted_text(p['text'], p['source_url']) if p.get('source_url') else p['text']
        assert text_units(wire) <= 4000


def test_readback_requires_original_source_hyperlink():
    expected = {'text': 'Оригинал\n\nИсточник', 'media': [], 'source_url': 'https://t.me/example/42'}
    message = {'body': {'text': expected['text'], 'attachments': [],
                       'markup': [{'type': 'link', 'url': expected['source_url']}]}}
    assert verify_message(message, expected)
    message['body']['markup'][0]['url'] = 'https://t.me/wrong/1'
    assert not verify_message(message, expected)
    message['body']['markup'] = []
    assert not verify_message(message, expected)


async def test_max_send_formats_only_source_and_keeps_attachment():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'message': {'body': {'mid': 'mid.formatted'}}})
    client = MaxClient('secret', client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await client.send(-1, '<b>Оригинал & 🙂</b>\n\nИсточник',
                          [{'type': 'image', 'payload': {'token': 'upload'}}],
                          source_url='https://t.me/example/42')
        assert seen[0]['format'] == 'html'
        assert seen[0]['attachments'] == [{'type': 'image', 'payload': {'token': 'upload'}}]
        assert '<b>' not in seen[0]['text']
        assert '<i><a href="https://t.me/example/42">Источник</a></i>' in seen[0]['text']
    finally:
        await client.aclose()


def test_source_metadata_refresh_is_not_an_edit_but_new_bytes_are(tmp_path):
    from PIL import Image
    path = tmp_path / 'source.png'
    Image.new('RGB', (20, 20), 'white').save(path)
    post = SimpleNamespace(id=1, message_id=2, text='Подпись', media_type='MessageMediaPhoto',
        is_deleted=False, grouped_id=None, raw={'media': {'file_reference': 'old'}},
        media_path=str(path), media_download_status='downloaded')
    chat = SimpleNamespace(peer_id=-1001, folder_name='MAX', chat_type='channel')
    snapshot = build_snapshot([post], chat, 'https://t.me/source/2', str(tmp_path))
    post.raw['media']['file_reference'] = 'refreshed'
    assert original_matches_snapshot([post], snapshot, str(tmp_path))
    Image.new('RGB', (20, 20), 'black').save(path)
    assert not original_matches_snapshot([post], snapshot, str(tmp_path))
    path.unlink()
    assert not original_matches_snapshot([post], snapshot, str(tmp_path))


def test_album_content_check_keeps_caption_order_and_detects_deletion():
    posts = [SimpleNamespace(id=i, message_id=i, text='Повтор' if i < 3 else 'Текст',
        media_type=None, is_deleted=False, grouped_id=1, raw={}, media_path=None,
        media_download_status=None) for i in range(1, 4)]
    chat = SimpleNamespace(peer_id=-1001, folder_name='MAX', chat_type='channel')
    snapshot = build_snapshot(posts, chat, 'https://t.me/source/1', '.')
    assert original_matches_snapshot(list(reversed(posts)), snapshot, '.')
    posts[2].text = 'Новая подпись'
    assert not original_matches_snapshot(posts, snapshot, '.')
    posts[2].text = 'Текст'
    assert not original_matches_snapshot(posts[:-1], snapshot, '.')
    posts[0].is_deleted = True
    assert not original_matches_snapshot(posts, snapshot, '.')
