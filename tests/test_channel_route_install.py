"""Report import must not enable an untested artifact or silently change a label."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from channel_corpus import TOPICS, FEATURES
from install_channel_routes import checked_routes
from app.content import selection_rules
from app.taxonomy.artifact import file_sha256
from app.publication.payload import sha_json
from app.publication.review_guard import GUARD_VERSION


def review_policy():
    return {'version': GUARD_VERSION, 'holds': {}, 'sha256': sha_json({})}


def bundle(tmp_path, monkeypatch):
    directory = tmp_path / 'weights'; directory.mkdir()
    (directory / 'baseline.joblib').write_bytes(b'QA weight bytes, not a trained model')
    small = directory / 'minilm-v3'; small.mkdir()
    (small / 'best.safetensors').write_bytes(b'QA checkpoint bytes, never run for inference')
    (small / 'training.json').write_text(json.dumps({'best_checkpoint': 'best.safetensors'}))
    models = {'tfidf': {'version': 'QA-tfidf', 'path': 'baseline.joblib',
        'weights_sha256': file_sha256(directory / 'baseline.joblib')},
        'minilm': {'version': 'QA-minilm', 'path': 'minilm-v3',
        'weights_sha256': file_sha256(small / 'best.safetensors')}}
    (directory / 'model-manifest.json').write_text(json.dumps({'models': models}))
    (directory / 'taxonomy.json').write_text(json.dumps({'version': TOPICS['version']}))
    catalog = {'version': TOPICS['version'], 'labels': [{'id': f, 'name': f} for f in FEATURES]}
    monkeypatch.setattr(selection_rules, 'taxonomy_catalog', lambda: catalog)
    monkeypatch.setattr('install_channel_routes.taxonomy_catalog', lambda: catalog)
    report = {'all_routes_ready': True, 'routes': {topic['id']: {
        'enabled': True, 'model_key': 'tfidf', 'model_version': 'QA-tfidf',
        'threshold': .73, 'context_threshold': .8, 'train_positive': 1000,
        'test_matched': 50, 'test_correct': 45, 'test_sha256': 'a' * 64,
        'split_sha256': 'b' * 64} for topic in TOPICS['topics']}}
    (directory / 'evaluation.json').write_text(json.dumps(report))
    return directory, report


def test_import_requires_all_routes_and_actual_weights(tmp_path, monkeypatch):
    directory, report = bundle(tmp_path, monkeypatch)
    rows = checked_routes(directory, review_policy())
    assert len(rows) == 9
    assert rows[0]['expression']['children'][0]['threshold'] == 73
    assert rows[0]['expression']['children'][1]['label_id'] == 'caption_has_context'
    (directory / 'baseline.joblib').write_bytes(b'changed weights')
    with pytest.raises(ValueError, match='weights do not match'): checked_routes(directory, review_policy())


def test_low_coverage_or_failed_route_cannot_be_imported(tmp_path, monkeypatch):
    directory, report = bundle(tmp_path, monkeypatch)
    report['routes']['is_joke']['train_positive'] = 999
    (directory / 'evaluation.json').write_text(json.dumps(report))
    with pytest.raises(ValueError): checked_routes(directory, review_policy())
    report['all_routes_ready'] = False
    (directory / 'evaluation.json').write_text(json.dumps(report))
    with pytest.raises(ValueError, match='Not all nine'): checked_routes(directory, review_policy())


def test_quality_report_alone_is_not_a_publication_content_review(tmp_path, monkeypatch):
    directory, _ = bundle(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='проверка содержимого'):
        checked_routes(directory)
