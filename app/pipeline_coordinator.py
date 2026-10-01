"""Короткие транзакции автоматической подготовки; модели работают отдельно."""

from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, or_
from fastapi import HTTPException
from app.config import get_settings
from app.db import create_engine, create_session_factory
from app.models import (
    PipelineEntry,
    TelegramPost,
    TelegramChat,
    SelectionFilter,
    SelectionFilterVersion,
    TaxonomyClassification,
)
from app.taxonomy.jobs import enqueue, text_sha256, mark_without_text
from app.content.selection_filters import (
    application_loop,
    assessment,
    has_marks,
    needs_backfill,
)
from app.content.post_preparation import mark_source, readiness_error
from app.runtime_status import heartbeat

logger = logging.getLogger(__name__)


def reset_retry(entry):
    entry.auto_attempts = 0
    entry.auto_retry_at = None
    entry.auto_phase = None
    entry.last_error = None


def failure(entry, phase, error, transient=False):
    if entry.auto_phase != phase:
        entry.auto_attempts = 0
    entry.auto_phase = phase
    entry.last_error = str(error)[:800]
    if transient and entry.auto_attempts < 3:
        entry.auto_retry_at = datetime.now(timezone.utc) + timedelta(
            seconds=(10, 30, 120)[entry.auto_attempts]
        )
        entry.auto_attempts += 1
        entry.auto_state = "pending"
    else:
        entry.auto_state = "stopped" if transient else "blocked"


async def advance(session, entry, post, chat, versions, settings):
    now = datetime.now(timezone.utc)
    if post.is_deleted:
        entry.auto_state = "stopped"
        entry.ready_at = None
        return
    jobs = {
        j.model_key: j
        for j in (
            await session.execute(
                select(TaxonomyClassification).where(
                    TaxonomyClassification.pipeline_entry_id == entry.id
                )
            )
        ).scalars()
    }
    if any(j.status in {"queued", "running"} for j in jobs.values()):
        entry.auto_retry_at = now + timedelta(seconds=2)
        return
    # Уже вручную маркированный пост не теряет одобрение из-за пустого словаря.
    manual = entry.auto_manual_mark and entry.marked_text_sha256 == text_sha256(
        post.text
    )
    required = (
        sorted({v.model_key for v in versions}, key=lambda key: key != "tfidf")
        if not manual
        else []
    )
    if not versions and not manual:
        entry.auto_retry_at = now + timedelta(seconds=30)
        entry.last_error = "Нет активных фильтров"
        return
    if not (post.text or "").strip() and not manual:
        await mark_without_text(session, entry, post)
        entry.auto_state = "done"
        return
    for key in required:
        job = jobs.get(key)
        if needs_backfill(job, post):
            if (
                job
                and job.status == "failed"
                and entry.auto_phase != f"retry:{job.current_run_id}"
            ):
                # Запоминаем конкретный провал, чтобы каждый polling не сдвигал таймер.
                entry.auto_phase = f"retry:{job.current_run_id}"
                entry.last_error = job.error or "Ошибка модели"
                if entry.auto_attempts >= 3:
                    entry.auto_state = "stopped"
                else:
                    entry.auto_retry_at = now + timedelta(
                        seconds=(10, 30, 120)[entry.auto_attempts]
                    )
                    entry.auto_attempts += 1
                return
            await enqueue(session, entry.id, key)
            entry.auto_retry_at = now + timedelta(seconds=2)
            return
    matched = any(
        assessment(v, jobs.get(v.model_key), post)["outcome"] == "matched"
        for v in versions
    )
    if not manual and (not matched or not await has_marks(session, entry.id)):
        entry.auto_state = "done"
        return
    if entry.stage in {"received", "sorted"}:
        entry.stage = "filtered"
        reset_retry(entry)
    elif entry.stage == "filtered":
        try:
            await mark_source(session, entry, post, chat)
            reset_retry(entry)
        except HTTPException as exc:
            failure(entry, "source", exc.detail)
    elif entry.stage == "marking":
        problem = await readiness_error(session, entry, post, settings.media_dir)
        if problem == "Альбом ещё собирается" or (problem and "pending" in problem):
            entry.auto_retry_at = now + timedelta(seconds=5)
            entry.last_error = problem
        elif problem:
            failure(entry, "media", problem)
        else:
            entry.stage, entry.status = "ready", "ready_to_publish"
            entry.ready_at = now
            entry.last_operation_at = now
            entry.auto_state = "done"
            reset_retry(entry)


async def coordinator_batch(factory, settings):
    async with factory() as session:
        ids = list(
            (
                await session.execute(
                    select(PipelineEntry.id)
                    .where(
                        PipelineEntry.auto_enabled.is_(True),
                        PipelineEntry.auto_state == "pending",
                        PipelineEntry.stage != "ready",
                        or_(
                            PipelineEntry.auto_retry_at.is_(None),
                            PipelineEntry.auto_retry_at <= datetime.now(timezone.utc),
                        ),
                    )
                    .order_by(PipelineEntry.id)
                    .limit(20)
                )
            ).scalars()
        )
    for entry_id in ids:
        async with factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(PipelineEntry, TelegramPost, TelegramChat)
                        .join(
                            TelegramPost,
                            TelegramPost.id == PipelineEntry.source_post_id,
                        )
                        .join(
                            TelegramChat,
                            TelegramChat.peer_id == TelegramPost.chat_peer_id,
                        )
                        .where(
                            PipelineEntry.id == entry_id,
                            TelegramChat.folder_name == "MAX",
                            PipelineEntry.auto_state == "pending",
                        )
                        .with_for_update(of=PipelineEntry, skip_locked=True)
                    )
                ).first()
                if not row:
                    continue
                versions = list(
                    (
                        await session.execute(
                            select(SelectionFilterVersion)
                            .join(
                                SelectionFilter,
                                SelectionFilter.active_version_id
                                == SelectionFilterVersion.id,
                            )
                            .where(
                                SelectionFilter.enabled.is_(True),
                                SelectionFilter.archived.is_(False),
                            )
                        )
                    ).scalars()
                )
                try:
                    async with session.begin_nested():
                        await advance(session, *row, versions, settings)
                except HTTPException as exc:
                    entry = await session.get(
                        PipelineEntry, entry_id, populate_existing=True
                    )
                    failure(entry, "queue", exc.detail, exc.status_code != 409)
                except Exception as exc:
                    entry = await session.get(
                        PipelineEntry, entry_id, populate_existing=True
                    )
                    failure(entry, "transition", type(exc).__name__, True)
                    logger.exception("card_transition_failed: %s", entry_id)


async def run():
    settings = get_settings()
    if (
        settings.auto_publish
        or settings.enable_external_llm
        or settings.enable_processing
    ):
        raise RuntimeError(
            "Coordinator only prepares posts; publishing and old processing must be off"
        )
    engine = create_engine(settings)
    factory = create_session_factory(engine=engine)
    filter_task = asyncio.create_task(application_loop(factory))
    try:
        while True:
            try:
                if filter_task.done():
                    raise RuntimeError("Filter application loop stopped")
                await coordinator_batch(factory, settings)
                await heartbeat(factory, "coordinator", success=True)
            except Exception as exc:
                logger.exception("coordinator_batch_failed")
                await heartbeat(factory, "coordinator", error=type(exc).__name__)
            await asyncio.sleep(2)
    finally:
        filter_task.cancel()
        await asyncio.gather(filter_task, return_exceptions=True)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
