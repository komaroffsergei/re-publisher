"""Single-process CPU worker for explicitly queued MAX taxonomy jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.models import PipelineEntry, TaxonomyClassification, TaxonomyRun, TelegramPost
from app.taxonomy.inference import TaxonomyModel
from app.taxonomy.jobs import MODEL_KEYS, text_sha256
from app.taxonomy.profiles import profile_of, source_of
from app.taxonomy.input_contract import InputNeedsReview


logger = logging.getLogger(__name__)


async def claim_next(factory, model_key: str) -> int | None:
    async with factory() as session:
        async with session.begin():
            job = (
                await session.execute(
                    select(TaxonomyClassification)
                    .where(TaxonomyClassification.status == "queued", TaxonomyClassification.model_key == model_key)
                    .order_by(TaxonomyClassification.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.attempts += 1
            job.started_at = datetime.now(timezone.utc)
            job.updated_at = job.started_at
            if job.current_run_id is not None:
                run = (await session.execute(
                    select(TaxonomyRun).where(TaxonomyRun.id == job.current_run_id).with_for_update()
                )).scalar_one()
                run.status = "running"
                run.started_at = job.started_at
            return job.id


async def finish_job(factory, job_id: int, result: dict | None, elapsed_ms: int, error: str | None = None, model_version: str | None = None) -> None:
    async with factory() as session:
        async with session.begin():
            # Совпадает с порядком блокировок enqueue/apply: карточка, затем job.
            entry_id = (await session.execute(select(TaxonomyClassification.pipeline_entry_id)
                .where(TaxonomyClassification.id == job_id))).scalar_one_or_none()
            if entry_id is None:
                return
            await session.execute(select(PipelineEntry.id).where(PipelineEntry.id == entry_id).with_for_update())
            job = (
                await session.execute(
                    select(TaxonomyClassification).where(TaxonomyClassification.id == job_id).with_for_update()
                )
            ).scalar_one_or_none()
            if job is None or job.status not in {"running", "loading"}:
                return
            post = (
                await session.execute(select(TelegramPost).where(TelegramPost.id == job.source_post_id))
            ).scalar_one_or_none()
            entry = (
                await session.execute(select(PipelineEntry).where(PipelineEntry.id == job.pipeline_entry_id))
            ).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            job.finished_at = now
            job.updated_at = now
            job.elapsed_ms = elapsed_ms
            if model_version is not None:
                job.model_version = model_version
            from app.ocr.jobs import classification_input_current
            ocr_stale = post is not None and not await classification_input_current(session, post, job, get_settings())
            if post is None or post.is_deleted or (source_of(job) != "ocr" and job.text_sha256 != text_sha256(post.text)) or ocr_stale:
                job.status = "stale"
                job.result = None
                job.error = None
                if entry is not None and entry.classification_id is None:
                    if post is not None and not (post.text or "").strip() and not post.is_deleted:
                        entry.stage = "sorted"
                        entry.status = "taxonomy_media_only" if post.media_type or post.media_path else "taxonomy_empty"
                    else:
                        other_complete = (await session.execute(
                            select(TaxonomyClassification.id).where(
                                TaxonomyClassification.pipeline_entry_id == entry.id,
                                TaxonomyClassification.id != job.id,
                                TaxonomyClassification.status == "complete",
                                TaxonomyClassification.text_sha256 == text_sha256(post.text if post else None),
                            ).limit(1)
                        )).scalar_one_or_none()
                        entry.stage = "sorted" if other_complete else "received"
                        entry.status = "taxonomy_sorted" if other_complete else "received"
            elif error is not None:
                job.status = "needs_review" if result and result.get("status") == "needs_review" else "failed"
                job.result = None
                job.error = error
                if entry is not None:
                    other_complete = (await session.execute(
                        select(TaxonomyClassification.id).where(
                            TaxonomyClassification.pipeline_entry_id == entry.id,
                            TaxonomyClassification.id != job.id,
                            TaxonomyClassification.status == "complete",
                            TaxonomyClassification.text_sha256 == job.text_sha256,
                        ).limit(1)
                    )).scalar_one_or_none()
                    entry.stage = "sorted" if other_complete else "received"
                    entry.status = "taxonomy_review" if job.status == "needs_review" else "taxonomy_failed"
            else:
                job.status = result.get("status", "complete") if result else "complete"
                job.result = result
                job.error = None
                if entry is not None:
                    entry.stage = "marking" if entry.marked_text_sha256 == job.text_sha256 else "sorted"
                    entry.status = "taxonomy_review" if result and result["review_status"] == "needs_review" else "taxonomy_sorted"
            if entry is not None:
                entry.last_operation_at = now
            if job.current_run_id is not None:
                run = (await session.execute(
                    select(TaxonomyRun).where(TaxonomyRun.id == job.current_run_id).with_for_update()
                )).scalar_one()
                run.status = job.status
                run.model_version = job.model_version
                run.result = job.result
                run.error = job.error
                run.elapsed_ms = job.elapsed_ms
                run.finished_at = now
            if entry is not None:
                from app.content.selection_filters import evaluate_completed_job, preserve_mark_stage
                if job.status == "complete":
                    await evaluate_completed_job(session, entry, post, job)
                else:
                    await preserve_mark_stage(session, entry)


async def model_call(function, *args):
    # Загрузка вынесена из event loop. В этот момент healthcheck видит живую
    # работу, но зависшая загрузка не продлевает heartbeat бесконечно.
    async def pulse():
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            Path("/tmp/taxonomy-worker-heartbeat").touch()
            await asyncio.sleep(5)
    task = asyncio.create_task(pulse())
    try:
        return await asyncio.to_thread(function, *args)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def run() -> None:
    settings = get_settings()
    if not settings.taxonomy_enabled:
        raise RuntimeError("TAXONOMY_ENABLED must be true for the taxonomy worker")
    model_key = os.environ.get("TAXONOMY_WORKER_MODEL", "tfidf")
    if model_key not in MODEL_KEYS:
        raise RuntimeError("TAXONOMY_WORKER_MODEL must be tfidf or minilm")
    model, loaded_profile = None, None
    engine = create_engine(settings)
    factory = create_session_factory(engine=engine)
    try:
        async with factory() as session:
            await session.execute(
                update(TaxonomyClassification)
                .where(TaxonomyClassification.status.in_(("running", "loading")), TaxonomyClassification.model_key == model_key)
                .values(status="queued", started_at=None)
            )
            await session.execute(
                update(TaxonomyRun)
                .where(TaxonomyRun.status.in_(("running", "loading")), TaxonomyRun.model_key == model_key)
                .values(status="queued", started_at=None)
            )
            await session.commit()
        while True:
            Path("/tmp/taxonomy-worker-heartbeat").touch()
            job_id = await claim_next(factory, model_key)
            if job_id is None:
                await asyncio.sleep(2)
                continue
            started = time.perf_counter()
            try:
                async with factory() as session:
                    row = (
                        await session.execute(
                            select(TaxonomyClassification, TelegramPost)
                            .join(TelegramPost, TelegramPost.id == TaxonomyClassification.source_post_id)
                            .where(TaxonomyClassification.id == job_id)
                        )
                    ).first()
                if row is None or (source_of(row[0]) != "ocr" and row[0].text_sha256 != text_sha256(row[1].text)) or row[1].is_deleted:
                    await finish_job(factory, job_id, None, 0)
                    continue
                profile = profile_of(row[0])
                model_input = row[1].text or ""
                if source_of(row[0]) != "text":
                    from app.ocr.jobs import current_input, classification_digest
                    async with factory() as session:
                        model_input, ocr_run = await current_input(session, row[0].pipeline_entry_id, row[1], settings, source_of(row[0]))
                    if model_input is None or not ocr_run or classification_digest(row[1], ocr_run, source_of(row[0])) != row[0].input_sha256:
                        await finish_job(factory, job_id, None, 0, "OCR устарел; повторите подготовку")
                        continue
                    if not model_input:
                        result = {"status": "media_only" if row[1].media_type else "empty", "category": "only_media" if row[1].media_type else "empty",
                            "top_3": [], "features": [], "scores": {}, "technical_complexity": None,
                            "review_status": "no_text", "profile": profile, "score_kind": "no_model_inference", "input_source": source_of(row[0])}
                        await finish_job(factory, job_id, result, 0)
                        continue
                if loaded_profile != profile:
                    # Один encoder в памяти. Сначала освобождаем предыдущий,
                    # затем загружаем новый, не держим два профиля одновременно.
                    import gc
                    model = None
                    gc.collect()
                    async with factory() as session:
                        await session.execute(update(TaxonomyClassification).where(TaxonomyClassification.id == job_id).values(status="loading"))
                        await session.execute(update(TaxonomyRun).where(TaxonomyRun.id == row[0].current_run_id).values(status="loading"))
                        await session.commit()
                    directory = settings.humor_model_dir if profile == "humor_ocr" else settings.taxonomy_model_dir
                    if model_key == "minilm":
                        from app.taxonomy.minilm import MiniLmTaxonomyModel
                        model = await model_call(MiniLmTaxonomyModel, directory)
                    else:
                        model = await model_call(TaxonomyModel, directory)
                    loaded_profile = profile
                    async with factory() as session:
                        await session.execute(update(TaxonomyClassification).where(TaxonomyClassification.id == job_id).values(status="running"))
                        await session.execute(update(TaxonomyRun).where(TaxonomyRun.id == row[0].current_run_id).values(status="running"))
                        await session.commit()
                started = time.perf_counter()
                result = await model_call(model.classify, model_input)
                await finish_job(factory, job_id, result, max(1, round((time.perf_counter() - started) * 1000)), model_version=model.model_version)
            except InputNeedsReview as exc:
                await finish_job(factory, job_id, {"status": "needs_review"}, 0, str(exc))
            except Exception as exc:
                logger.error("taxonomy classification failed: job_id=%s type=%s", job_id, type(exc).__name__)
                await finish_job(factory, job_id, None, max(1, round((time.perf_counter() - started) * 1000)),
                                 "Ошибка модели; можно повторить")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
