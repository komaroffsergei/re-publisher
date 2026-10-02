"""Настоящая БД: история OCR, два профиля и отсутствие массового backfill."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
from pathlib import Path
import pytest
from sqlalchemy import select, func
import test_selection_integration as shared
from app.config import get_settings
from app.models import PipelineEntry, TelegramPost, TaxonomyClassification, FilterApplication
from app.ocr.models import OcrJob, OcrRun
from app.ocr.jobs import enqueue_ocr, current_input, invalidate_ocr
from app.ocr.worker import claim, process
from app.taxonomy.jobs import enqueue

db = shared.db
client = shared.client
pytestmark = shared.pytestmark


def configure(tmp_path):
    settings = get_settings()
    settings.ocr_enabled = True
    settings.humor_model_dir = str(tmp_path / "humor")
    settings.ocr_model_dir = str(tmp_path / "ocr-models")
    settings.media_dir = str(tmp_path / "media")
    settings.ocr_preview_dir = str(tmp_path / "previews")
    settings.cache_dir = str(tmp_path / "cache")
    Path(settings.ocr_model_dir).mkdir()
    Path(settings.ocr_model_dir, "ocr-manifest.json").write_text(json.dumps({"models": {}}))
    Path(settings.media_dir).mkdir()
    return settings


async def media_post(db, entry_id, settings):
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        path = Path(settings.media_dir) / "fixture.png"; path.write_bytes(b"different input")
        post.text, post.media_path, post.media_type = None, str(path), "MessageMediaPhoto"
        post.media_download_status, post.updated_at = "downloaded", datetime.now(timezone.utc) - timedelta(seconds=10)
        await session.commit()


class Reader:
    version = "fixture-no-model-inference"
    async def read(self, item):
        return {"status": "complete", "blocks": [{"text": "Работаю над багами: они размножаются", "score": .99, "box": [[0,0],[10,0],[10,10],[0,10]]}],
            "text": "Работаю над багами: они размножаются", "media_sha256": item["sha256"], "engine_version": self.version, "elapsed_ms": 10}


async def test_ocr_empty_caption_queues_only_requested_profile_and_retains_taxonomy(db, client, tmp_path):
    entry_id = await shared.seed(db)
    settings = configure(tmp_path)
    await media_post(db, entry_id, settings)
    async with db() as session:
        job = await enqueue(session, entry_id, "tfidf", "humor_ocr")
        assert job.status == "ocr" and job.profile == "humor_ocr"
        await session.commit()
    await process(db, settings, Reader(), *(await claim(db)))
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        value, run = await current_input(session, entry_id, post, settings)
        assert "размножаются" in value and post.text is None
        jobs = list((await session.execute(select(TaxonomyClassification).where(TaxonomyClassification.pipeline_entry_id == entry_id))).scalars())
        assert len(jobs) == 3
        humor = next(j for j in jobs if j.profile == "humor_ocr")
        assert humor.status == "queued" and humor.ocr_run_id == run.id
        assert all(j.status == "complete" for j in jobs if j.profile == "taxonomy")
    data = (await client.get(f"/api/pipeline/{entry_id}/ocr")).json()
    assert data["status"] == "complete" and data["runs"][0]["current"]
    assert (await client.post(f"/api/pipeline/{entry_id}/ocr", json={}, auth=None)).status_code == 401
    assert (await client.post(f"/api/pipeline/{entry_id}/ocr", json={}, headers={"Origin":"https://outside.invalid"})).status_code == 403


async def test_changed_media_invalidates_result_but_keeps_history(db, client, tmp_path):
    entry_id = await shared.seed(db)
    settings = configure(tmp_path)
    await media_post(db, entry_id, settings)
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        await enqueue_ocr(session, entry, post, settings)
        await session.commit()
    await process(db, settings, Reader(), *(await claim(db)))
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        Path(post.media_path).write_bytes(b"changed media")
        await invalidate_ocr(session, post, settings)
        await session.commit()
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        assert (await current_input(session, entry_id, post, settings))[0] is None
        assert (await session.execute(select(func.count(OcrRun.id)))).scalar_one() == 1
        assert (await session.get(OcrJob, entry_id)).status == "stale"


async def test_humor_filter_does_not_enqueue_ocr_for_old_buffer(db, client):
    await shared.seed(db)
    mark = (await client.post("/api/pipeline/marks", json={"name":"Смешное"})).json()["id"]
    draft = {"name":"Humor", "profile":"humor_ocr", "model_key":"tfidf", "mark_id":mark,
        "expression":{"op":"and", "children":[{"op":"condition","label_id":key,"compare":"gte","threshold":92}
                      for key in ("is_joke","input_has_context")]}}
    preview = await client.post("/api/pipeline/filters/preview", json=draft)
    assert preview.status_code == 200
    blocked = await client.post("/api/pipeline/filters/apply", json={**draft,"preview_digest":preview.json()["preview_digest"]})
    assert blocked.status_code == 409
    # Ручной эксперимент сохраняется выключенным до прохождения допуска.
    disabled = {**draft, "enabled": False}
    preview = await client.post("/api/pipeline/filters/preview", json=disabled)
    saved = await client.post("/api/pipeline/filters/apply", json={**disabled,"preview_digest":preview.json()["preview_digest"]})
    assert saved.status_code == 200 and saved.json()["application_id"] is None
    # Даже после явного допуска профиль не подхватывает старый буфер.
    get_settings().humor_auto_enabled = True
    preview = await client.post("/api/pipeline/filters/preview", json=draft)
    result = await client.post("/api/pipeline/filters/apply", json={**draft,"preview_digest":preview.json()["preview_digest"]})
    assert result.status_code == 200 and result.json()["application_id"] is None
    async with db() as session:
        assert (await session.execute(select(func.count(OcrJob.entry_id)))).scalar_one() == 0
        assert (await session.execute(select(func.count(FilterApplication.id)))).scalar_one() == 0


async def test_media_changed_outside_collector_cannot_pass_filter_or_readiness(db, client, tmp_path):
    from app.content.selection_filters import current_assessment
    from app.content.post_preparation import readiness_error
    from app.taxonomy.jobs import text_sha256
    entry_id = await shared.seed(db)
    settings = configure(tmp_path)
    await media_post(db, entry_id, settings)
    async with db() as session:
        await enqueue(session, entry_id, "tfidf", "humor_ocr")
        await session.commit()
    await process(db, settings, Reader(), *(await claim(db)))
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        job = (await session.execute(select(TaxonomyClassification).where(
            TaxonomyClassification.pipeline_entry_id == entry_id,
            TaxonomyClassification.profile == "humor_ocr"))).scalar_one()
        job.status = "complete"
        job.result = {"taxonomy_version":"humor_ocr_v1", "scores":{"is_joke":.99,"input_has_context":.99}}
        version = SimpleNamespace(profile="humor_ocr", expression={"op":"condition", "label_id":"is_joke", "compare":"gte", "threshold":92})
        assert (await current_assessment(session, version, job, post))["outcome"] == "matched"
        # Collector ещё не получил событие, подпись не менялась, оценки высокие.
        Path(post.media_path).write_bytes(b"replacement attachment")
        assert (await current_assessment(session, version, job, post))["outcome"] == "unknown"
        entry.marked_source_url = "https://t.me/example/1"
        entry.marked_text_sha256 = text_sha256(post.text)
        assert "OCR" in await readiness_error(session, entry, post, settings.media_dir)


async def test_unchanged_video_preview_does_not_hide_changed_original(db, client, tmp_path):
    from app.ocr.jobs import snapshot
    entry_id = await shared.seed(db)
    settings = configure(tmp_path)
    await media_post(db, entry_id, settings)
    Path(settings.ocr_preview_dir).mkdir()
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        preview = Path(settings.ocr_preview_dir) / "preview.jpg"
        preview.write_bytes(b"unchanged preview")
        post.media_type = "MessageMediaDocument"
        post.raw = {"media":{"document":{"id":123, "mime_type":"video/mp4"}}}
        post.ocr_preview_path, post.ocr_preview_status = str(preview), "downloaded"
        first = (await snapshot(session, post, settings))[0]
        Path(post.media_path).write_bytes(b"replacement original")
        assert (await snapshot(session, post, settings))[0] != first


async def test_missing_media_stops_after_bounded_retries_and_allows_explicit_retry(db, client, tmp_path):
    entry_id = await shared.seed(db)
    settings = configure(tmp_path)
    await media_post(db, entry_id, settings)
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        Path(post.media_path).unlink()
        await enqueue(session, entry_id, "tfidf", "humor_ocr")
        await session.commit()
    for attempt, delay in enumerate((10, 30, 120, None), 1):
        picked = await claim(db)
        assert picked is not None
        await process(db, settings, Reader(), *picked)
        async with db() as session:
            job = await session.get(OcrJob, entry_id)
            assert job.attempts == attempt
            if delay:
                assert job.status == "queued"
                assert delay - 3 <= (job.retry_at - datetime.now(timezone.utc)).total_seconds() <= delay
                job.retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await session.commit()
            else:
                assert job.status == "failed"
                classification = (await session.execute(select(TaxonomyClassification).where(
                    TaxonomyClassification.pipeline_entry_id == entry_id,
                    TaxonomyClassification.profile == "humor_ocr"))).scalar_one()
                assert classification.status == "failed" and classification.finished_at
                entry = await session.get(PipelineEntry, entry_id)
                entry.auto_enabled, entry.auto_state = True, "stopped"
                await session.commit()
    assert await claim(db) is None
    response = await client.post(f"/api/pipeline/{entry_id}/automation/retry", json={})
    assert response.status_code == 200
    async with db() as session:
        assert (await session.get(OcrJob, entry_id)).attempts == 0


async def test_deleted_album_member_blocks_ocr_and_ready(db, client, tmp_path):
    from app.ocr.jobs import snapshot
    from app.content.post_preparation import readiness_error
    from app.taxonomy.jobs import text_sha256
    entry_id = await shared.seed(db)
    settings = configure(tmp_path)
    await media_post(db, entry_id, settings)
    async with db() as session:
        entry = await session.get(PipelineEntry, entry_id)
        post = await session.get(TelegramPost, entry.source_post_id)
        post.grouped_id = 999
        session.add(TelegramPost(chat_peer_id=post.chat_peer_id, message_id=2,
            grouped_id=999, text=None, date=datetime.now(timezone.utc), is_deleted=True, raw={}))
        await session.flush()
        assert "удалено" in (await snapshot(session, post, settings))[2]
        entry.marked_source_url, entry.marked_text_sha256 = "https://t.me/example/1", text_sha256(post.text)
        assert "удалено" in await readiness_error(session, entry, post, settings.media_dir)
