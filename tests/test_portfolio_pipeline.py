from app.portfolio_pipeline import CORPUS, run_pipeline


def test_real_pipeline_and_determinism():
    for fixture in CORPUS:
        first = run_pipeline(fixture['id'])
        second = run_pipeline(fixture['id'])
        assert first['status'] == 'draft'
        assert len(first['extracted']['extracted_text']) > 100
        assert first['classification'] == second['classification']
        assert first['classification']['label'] == fixture['label']
        assert first['draft'] == second['draft']
        assert first['draft']['source_url'].startswith('https://publisher.komaroff-dev.ru/source/')


def test_controlled_failure_can_be_retried():
    failed = run_pipeline('tool', True)
    assert failed['status'] == 'failed'
    assert 'draft' not in failed
    assert failed['stages'][-1]['state'] == 'failed'
    assert run_pipeline('tool')['status'] == 'draft'
