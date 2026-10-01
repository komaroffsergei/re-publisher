import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from channel_corpus import FEATURES, TOPICS, connect, import_snapshot, next_batch, record
from freeze_channel_training import freeze, group_and_split, split_for
from train_channel_models import known, validate_dataset


def seed(tmp_path, texts):
    directory = tmp_path / 'private'; db = connect(directory)
    snapshot = tmp_path / 'source.jsonl'
    snapshot.write_text(''.join(json.dumps({'id': i, 'chat_peer_id': -1001, 'message_id': i,
        'text': text, 'chat_type': 'channel', 'has_media': False}) + '\n' for i, text in enumerate(texts, 1)))
    import_snapshot(db, snapshot)
    return db


def test_import_deduplicates_without_inventing_labels(tmp_path):
    db = seed(tmp_path, ['same text', 'same  text', 'different text'])
    try:
        assert db.execute('SELECT COUNT(*) FROM texts').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM annotations').fetchone()[0] == 0
        assert len(next_batch(db, 20, 'same', None)) == 1
    finally: db.close()


def test_caption_without_context_cannot_fill_positive_quota(tmp_path):
    db = seed(tmp_path, ['look at this'])
    try:
        sha = db.execute('SELECT sha FROM texts').fetchone()[0]
        data = {'sha': sha, 'version': TOPICS['version'], 'provenance': 'codex_agent',
            'labels': {name: 'no' for name in FEATURES}, 'reasons': {'is_joke': 'fixture'}}
        data['labels']['is_joke'] = 'yes'
        batch = tmp_path / 'manual.jsonl'; batch.write_text(json.dumps(data) + '\n')
        with pytest.raises(ValueError, match='Insufficient caption'): record(db, batch)
        assert db.execute('SELECT COUNT(*) FROM annotations').fetchone()[0] == 0
    finally: db.close()


def test_candidate_search_handles_russian_case_and_channel_boundary(tmp_path):
    db = seed(tmp_path, ['Исследование модели', 'исследование в групповом разговоре'])
    try:
        db.execute("UPDATE sources SET chat_type='supergroup' WHERE message=2")
        assert len(next_batch(db, 20, 'ИССЛЕДОВАНИЕ', None)) == 2
        selected = next_batch(db, 20, 'исследование', None, publishable_only=True)
        assert [row['text'] for row in selected] == ['Исследование модели']
        assert db.execute('SELECT COUNT(*) FROM annotations').fetchone()[0] == 0
    finally: db.close()


def test_similar_materials_stay_in_same_partition(tmp_path):
    text = ' '.join('word' + str(i) for i in range(80))
    db = seed(tmp_path, [text, text + ' updated details', ' '.join('other' + str(i) for i in range(80))])
    try:
        group_and_split(db)
        rows = list(db.execute('SELECT text,group_id,split FROM texts'))
        near = [r for r in rows if r['text'].startswith('word0')]
        assert near[0]['group_id'] == near[1]['group_id']
        assert near[0]['split'] == near[1]['split'] == split_for(near[0]['group_id'])
    finally: db.close()


def test_previously_reserved_post_and_variants_remain_in_test(tmp_path):
    text = ' '.join('word' + str(i) for i in range(80))
    db = seed(tmp_path, [text, text + ' revised caption'])
    reserved = tmp_path / 'reserved.jsonl'
    reserved.write_text(json.dumps({'chat_peer_id': -1001, 'message_id': 1,
        'text_sha256': hashlib.sha256(text.encode()).hexdigest()}) + '\n')
    try:
        result = group_and_split(db, [reserved])
        assert result['reserved_test_groups'] == 1
        assert {r['split'] for r in db.execute('SELECT split FROM texts')} == {'test'}
        # Повтор группировки без аргумента не снимает прежнее резервирование.
        group_and_split(db)
        assert {r['split'] for r in db.execute('SELECT split FROM texts')} == {'test'}
    finally: db.close()


def test_insufficient_quota_prevents_training_snapshot(tmp_path):
    db = seed(tmp_path, ['readable sample'])
    try:
        group_and_split(db)
        with pytest.raises(ValueError, match='quotas not reached'): freeze(db, tmp_path / 'frozen')
        assert not (tmp_path / 'frozen').exists()
    finally: db.close()


def test_unknown_and_missing_labels_are_not_negative_training_targets():
    indices, target = known([{'labels': {'j': 'yes'}}, {'labels': {'j': 'unclear'}}, {'labels': {}}, {'labels': {'j': 'no'}}], 'j')
    assert indices.tolist() == [0, 3]
    assert target.tolist() == [1, 0]


def test_frozen_dataset_changes_are_rejected(tmp_path):
    (tmp_path / 'dataset.jsonl').write_bytes(b'changed')
    (tmp_path / 'dataset-manifest.json').write_text(json.dumps({'dataset_sha256': hashlib.sha256(b'original').hexdigest()}))
    with pytest.raises(ValueError, match='changed after freezing'): validate_dataset(tmp_path)
