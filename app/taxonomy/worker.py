"""Single-process CPU worker for explicitly queued MAX taxonomy jobs."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.models import PipelineEntry, TaxonomyClassification, TelegramPost
from app.taxonomy.inference import TaxonomyModel
from app.taxonomy.jobs import text_sha256


logger = logging.getLogger(__name__)


async def claim_next(factory) -> int | None:
    async with factory() as session:
        async with session.begin():
            job = (
                await session.execute(
                    select(TaxonomyClassification)
                    .where(TaxonomyClassification.status == "queued")
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
            return job.id


async def finish_job(factory, job_id: int, result: dict | None, elapsed_ms: int, error: str | None = None) -> None:
    async with factory() as session:
        async with session.begin():
            job = (
                await session.execute(
                    select(TaxonomyClassification).where(TaxonomyClassification.id == job_id).with_for_update()
                )
            ).scalar_one_or_none()
            if job is None or job.status != "running":
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
            if post is None or post.is_deleted or job.text_sha256 != text_sha256(post.text):
                job.status = "stale"
                job.result = None
                job.error = None
                if entry is not None and entry.classification_id is None:
                    entry.stage = "received"
                    entry.status = "received"
            elif error is not None:
                job.status = "failed"
                job.result = None
                job.error = error
                if entry is not None:
                    entry.stage = "received"
                    entry.status = "taxonomy_failed"
            else:
                job.status = "complete"
                job.result = result
                job.error = None
                if entry is not None:
                    entry.stage = "sorted"
                    entry.status = "taxonomy_review" if result and result["review_status"] == "needs_review" else "taxonomy_sorted"
            if entry is not None:
                entry.last_operation_at = now


async def run() -> None:
    settings = get_settings()
    if not settings.taxonomy_enabled:
        raise RuntimeError("TAXONOMY_ENABLED must be true for the taxonomy worker")
    model = TaxonomyModel(settings.taxonomy_model_dir)
    engine = create_engine(settings)
    factory = create_session_factory(engine=engine)
    try:
        async with factory() as session:
            await session.execute(
                update(TaxonomyClassification)
                .where(TaxonomyClassification.status == "running")
                .values(status="queued", started_at=None)
            )
            await session.commit()
        while True:
            Path("/tmp/taxonomy-worker-heartbeat").touch()
            job_id = await claim_next(factory)
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
                if row is None or row[0].text_sha256 != text_sha256(row[1].text) or row[1].is_deleted:
                    await finish_job(factory, job_id, None, 0)
                    continue
                result = await asyncio.to_thread(model.classify, row[1].text or "")
                await finish_job(factory, job_id, result, max(1, round((time.perf_counter() - started) * 1000)))
            except Exception as exc:
                logger.error("taxonomy classification failed: job_id=%s type=%s", job_id, type(exc).__name__)
                await finish_job(factory, job_id, None, max(1, round((time.perf_counter() - started) * 1000)),
                                 "Ошибка модели; можно повторить")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
