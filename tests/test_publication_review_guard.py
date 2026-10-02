from copy import deepcopy

import pytest

from app.publication.payload import sha_json
from app.publication.review_guard import GUARD_VERSION, caption_sha, hold_reason, policy_error


def policy(holds=None):
    holds = holds or {}
    return {'version': GUARD_VERSION, 'holds': holds, 'sha256': sha_json(holds)}


def snapshot(text, links=''):
    return {'original_text': text, 'text': text + links}


def test_exact_read_post_and_whitespace_copy_share_a_hold_without_changing_text():
    text = 'An explicitly reviewed post'
    data = snapshot('  An explicitly\nreviewed  post ')
    before = deepcopy(data)
    reason = hold_reason(data, policy({caption_sha(text): 'Owner-side review needed'}))
    assert 'Owner-side review' in reason
    assert data == before


def test_album_caption_hold_cannot_be_hidden_by_another_caption():
    data = snapshot('Ordinary first caption\n\nReviewed second caption')
    data['original_captions'] = ['Ordinary first caption', 'Reviewed second caption']
    assert hold_reason(data, policy({caption_sha('Reviewed second caption'): 'Reviewed hold'}))


@pytest.mark.parametrize('text', ['Школьница и сексуальная картинка', 'Обнажённая девушка 15 лет', 'A teenage girl in a nude image', 'Раздеватор для фото', 'Undress photo tool'])
def test_sensitive_context_waits_for_review(text):
    assert hold_reason(snapshot(text), policy())


@pytest.mark.parametrize('text', ['ИИ помогает школьнику разбираться в математике', 'Мультфильм про взрослого робота', 'Модель распознаёт одежду на фотографиях', 'Photos for a school education project'])
def test_unrelated_educational_context_is_not_a_sensitive_match(text):
    assert hold_reason(snapshot(text), policy()) is None


def test_hidden_link_access_token_is_held_without_copying_the_secret_into_error():
    token = 'never-forward-this-credential-value'
    reason = hold_reason(snapshot('A useful tool', '\nhttps://example.test/?access_token=' + token), policy())
    assert reason and token not in reason and 'example.test' not in reason
    assert hold_reason(snapshot('Example', '\nhttps://example.test/?api_key=YOUR_TOKEN_PLACEHOLDER_12345'), policy()) is None
    assert hold_reason(snapshot('Tool', '\nhttps://example.test/?model=minilm&chat_id=-123456'), policy()) is None


@pytest.mark.parametrize('change', [
    {'version': 'other'}, {'sha256': 'incorrect'}, {'holds': {'bad': 'reason'}},
    {'holds': {1: 'reason'}}, {'holds': {'a' * 64: ''}},
])
def test_missing_or_changed_review_policy_is_not_silently_accepted(change):
    rule = policy(); rule.update(change)
    assert policy_error(rule)
    assert policy_error(None)
