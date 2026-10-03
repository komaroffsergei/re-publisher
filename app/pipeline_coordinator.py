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
from app.taxonomy.profiles import profile_of, job_key, source_of
from app.content.selection_filters import (
    application_loop,
    current_assessment,
    evaluate_post,
    has_marks,
    needs_backfill,
)
from app.content.post_preparation import mark_source, readiness_error
from app.runtime_status import heartbeat
from app.content.selection_rules import evaluate, required_sources

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
    # Ручной OCR доступен раньше автоматического отбора. Допуск выставляется
    # отдельно после проверки корпуса, качества и ресурсов, не из интерфейса.
    versions = [v for v in versions if profile_of(v) != "humor_ocr"
                or settings.humor_auto_enabled]
    now = datetime.now(timezone.utc)
    if post.is_deleted:
        entry.auto_state = "stopped"
        entry.ready_at = None
        return
    jobs = {
        job_key(j.model_key, profile_of(j), source_of(j)): j
        for j in (
            await session.execute(
                select(TaxonomyClassification).where(
                    TaxonomyClassification.pipeline_entry_id == entry.id
                )
            )
        ).scalars()
    }
    if any(j.status in {"ocr", "queued", "loading", "running"} for j in jobs.values()):
        entry.auto_retry_at = now + timedelta(seconds=2)
        return
    # Уже вручную маркированный пост не теряет одобрение из-за пустого словаря.
    manual = entry.auto_manual_mark and entry.marked_text_sha256 == text_sha256(
        post.text
    )
    if not versions and not manual:
        entry.auto_retry_at = now + timedelta(seconds=30)
        entry.last_error = "Нет активных фильтров"
        return
    if not (post.text or "").strip() and not manual and not any(
        required_sources(version) & {"ocr", "combined"} for version in versions):
        await mark_without_text(session, entry, post)
        entry.auto_state = "done"
        return
    matched, problems = False, []
    for version in ([] if manual else versions):
        # Длина известна до inference. Ложная ветка И не запускает лишний OCR.
        preliminary, _ = evaluate(version.expression, {}, len((post.text or "").strip()))
        if preliminary is False:
            continue
        inputs = {}
        blocked = False
        for source in sorted(required_sources(version), key=lambda x: x != "text"):
            key, profile = version.model_key, profile_of(version)
            job = jobs.get(job_key(key, profile, source))
            if job and source != "text" and job.status in {"complete", "media_only", "empty"}:
                from app.ocr.jobs import classification_input_current
                if not await classification_input_current(session, post, job, settings):
                    job.status = "stale"
            if job and job.status in {"needs_review", "failed"}:
                if job.status == "needs_review" or source != "text" and job.status == "failed":
                    problems.append(job.error or "OCR требует ручного разбора")
                    blocked = True
                    break
                if entry.auto_phase != f"retry:{job.current_run_id}":
                    entry.auto_phase = f"retry:{job.current_run_id}"
                    if entry.auto_attempts >= 3:
                        problems.append(job.error or "Ошибка модели")
                        blocked = True
                        break
                    entry.auto_retry_at = now + timedelta(seconds=(10, 30, 120)[entry.auto_attempts])
                    entry.auto_attempts += 1
                    return
            if needs_backfill(job, post, profile, source):
                await enqueue(session, entry.id, key, profile, source)
                entry.auto_retry_at = now + timedelta(seconds=2)
                return
            inputs[source] = job
        if blocked:
            continue
        values = await evaluate_post(session, entry, post, version, f"auto:{text_sha256(post.text)}", inputs)
        matched = matched or values["outcome"] == "matched"
    if not matched and not manual and problems:
        exhausted = entry.auto_attempts >= 3
        failure(entry, "ocr", "; ".join(dict.fromkeys(problems)))
        if exhausted:
            entry.auto_state = "stopped"
        return
    if not (post.text or "").strip() and not any(j.status == "complete" for j in jobs.values()) and not manual:
        await mark_without_text(session, entry, post)
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
