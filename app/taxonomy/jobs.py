"""Queue the current state and preserve each model run without storing post text."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    PipelineEntry,
    TaxonomyClassification,
    TaxonomyRun,
    TelegramChat,
    TelegramPost,
)

MODEL_KEYS = ('tfidf', 'minilm')


def text_sha256(text: str | None) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def public_job(
    job: TaxonomyClassification | None, current_text: str | None = None
) -> dict | None:
    if job is None:
        return None
    status = (
        "stale"
        if current_text is not None and job.text_sha256 != text_sha256(current_text)
        else job.status
    )
    return {
        "run_id": getattr(job, "current_run_id", None),
        "model_key": job.model_key,
        "status": status,
        "model_version": job.model_version,
        "result": job.result if status in {"complete", "media_only", "empty"} else None,
        "error": job.error if status == "failed" else None,
        "elapsed_ms": job.elapsed_ms
        if status in {"complete", "media_only", "empty"}
        else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


async def mark_without_text(
    session: AsyncSession, entry: PipelineEntry, post: TelegramPost
) -> TaxonomyClassification | None:
    """Immediately sort textless posts; semantic workers never see them."""
    if (post.text or "").strip():
        return None
    if entry.stage == 'published':
        return None
    fingerprint = text_sha256(post.text)
    job = (
        await session.execute(
            select(TaxonomyClassification)
            .where(
                TaxonomyClassification.pipeline_entry_id == entry.id,
                TaxonomyClassification.model_key == "media",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    has_media = bool(post.media_type or post.media_path)
    status = "media_only" if has_media else "empty"
    if job is None:
        job = TaxonomyClassification(
            pipeline_entry_id=entry.id,
            source_post_id=post.id,
            model_key="media",
            text_sha256=fingerprint,
        )
        session.add(job)
    if (
        job.status != status
        or job.text_sha256 != fingerprint
        or entry.stage not in {"sorted", "filtered", "marking", "ready"}
        or (job.result or {}).get("category")
        != ("only_media" if has_media else "empty")
    ):
        now = datetime.now(timezone.utc)
        job.text_sha256 = fingerprint
        job.model_version = None
        job.status = status
        job.result = {
            "category": "only_media" if has_media else "empty",
            "top_3": [],
            "features": [],
            "technical_complexity": None,
            "review_status": "no_text",
            "score_kind": "no_model_inference",
            "media": has_media,
        }
        job.error = None
        job.elapsed_ms = 0
        job.started_at = None
        job.finished_at = now
        job.updated_at = now
        entry.stage = "marking" if entry.marked_text_sha256 == fingerprint else "sorted"
        entry.status = "taxonomy_media_only" if has_media else "taxonomy_empty"
        entry.last_operation_at = now
        from app.content.selection_filters import preserve_mark_stage

        await preserve_mark_stage(session, entry)
    await session.flush()
    return job


async def sort_textless_post(
    session: AsyncSession, post_id: int
) -> TaxonomyClassification | None:
    row = (
        await session.execute(
            select(PipelineEntry, TelegramPost)
            .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
            .where(TelegramPost.id == post_id)
            .with_for_update(of=PipelineEntry)
        )
    ).first()
    return await mark_without_text(session, *row) if row else None


async def enqueue(
    session: AsyncSession, entry_id: int, model_key: str
) -> TaxonomyClassification:
    if model_key not in MODEL_KEYS:
        raise HTTPException(404, detail="Модель не найдена")
    row = (
        await session.execute(
            select(PipelineEntry, TelegramPost, TelegramChat)
            .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
            .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
            .where(PipelineEntry.id == entry_id)
            .with_for_update(of=PipelineEntry)
        )
    ).first()
    if row is None:
        raise HTTPException(404, detail="Карточка не найдена")
    entry, post, chat = row
    if (
        chat.folder_name != "MAX"
        or post.is_deleted
        or entry.stage not in {"received", "sorted", "filtered", "marking", "ready"}
    ):
        raise HTTPException(409, detail="Карточка недоступна для сортировки")
    if not (post.text or "").strip():
        return await mark_without_text(session, entry, post)

    job = (
        await session.execute(
            select(TaxonomyClassification)
            .where(
                TaxonomyClassification.pipeline_entry_id == entry_id,
                TaxonomyClassification.model_key == model_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    fingerprint = text_sha256(post.text)
    if (
        job is not None
        and job.status in {"queued", "running"}
        and job.text_sha256 == fingerprint
    ):
        return job
    other_active = (
        await session.execute(
            select(TaxonomyClassification.id)
            .where(
                TaxonomyClassification.pipeline_entry_id == entry_id,
                TaxonomyClassification.model_key != model_key,
                TaxonomyClassification.status.in_(("queued", "running")),
                TaxonomyClassification.text_sha256 == fingerprint,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if other_active is not None:
        raise HTTPException(409, detail="Карточка уже обрабатывается другой моделью")
    now = datetime.now(timezone.utc)
    if job is None:
        job = TaxonomyClassification(
            pipeline_entry_id=entry.id,
            source_post_id=post.id,
            model_key=model_key,
            text_sha256=fingerprint,
        )
        session.add(job)
    job.text_sha256 = fingerprint
    # Worker запишет версию фактически загруженных весов. До этого она неизвестна.
    job.model_version = None
    run = TaxonomyRun(
        pipeline_entry_id=entry.id,
        source_post_id=post.id,
        model_key=model_key,
        text_sha256=fingerprint,
        model_version=None,
        status="queued",
        queued_at=now,
    )
    session.add(run)
    await session.flush()
    job.current_run_id = run.id
    job.status = "queued"
    job.result = None
    job.error = None
    job.elapsed_ms = None
    job.started_at = None
    job.finished_at = None
    job.updated_at = now
    if entry.stage == "ready":
        entry.stage = "marking"
        entry.ready_at = None
    if entry.auto_enabled:
        entry.auto_state = "pending"
        entry.auto_retry_at = None
    entry.last_operation_at = now
    await session.flush()
    return job


async def invalidate_if_edited(
    session: AsyncSession, post_id: int, text: str | None
) -> bool:
    fingerprint = text_sha256(text)
    entry = (
        await session.execute(
            select(PipelineEntry)
            .where(PipelineEntry.source_post_id == post_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    jobs = list(
        (
            await session.execute(
                select(TaxonomyClassification)
                .where(TaxonomyClassification.source_post_id == post_id)
                .with_for_update()
            )
        ).scalars()
    )
    changed = [job for job in jobs if job.text_sha256 != fingerprint]
    marked_stale = bool(
        entry and entry.marked_text_sha256 and entry.marked_text_sha256 != fingerprint
    )
    if not changed and not marked_stale:
        return False
    now = datetime.now(timezone.utc)
    for job in changed:
        if job.current_run_id is not None and job.status in {"queued", "running"}:
            run = (
                await session.execute(
                    select(TaxonomyRun)
                    .where(TaxonomyRun.id == job.current_run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is not None:
                run.status = "stale"
                run.finished_at = now
        job.status = "stale"
        job.result = None
        job.error = None
        job.updated_at = now
    if entry is not None:
        entry.ready_at = None
        entry.auto_manual_mark = False
        if entry.stage == 'published':
            # История доставки сохраняется. Редактирование не создаёт новую
            # публикацию уже отправленного источника.
            entry.auto_state = 'done'
            entry.status = 'published_source_changed'
        elif entry.auto_enabled:
            entry.auto_state = "pending"
            entry.auto_attempts = 0
            entry.auto_retry_at = None
            entry.auto_phase = None
            entry.last_error = None
        if marked_stale:
            entry.marked_text = None
            entry.marked_source_url = None
            entry.marked_text_sha256 = None
            entry.marked_at = None
        if (
            entry.stage in {"sorted", "filtered", "marking", "ready"}
            and entry.classification_id is None
        ):
            entry.stage = "received"
            entry.status = "received"
            entry.last_operation_at = now
            from app.content.selection_filters import preserve_mark_stage

            await preserve_mark_stage(session, entry)
    return True


def public_run(run: TaxonomyRun, current_text: str | None) -> dict:
    return {
        "id": run.id,
        "model_key": run.model_key,
        "model_version": run.model_version,
        "status": run.status,
        "result": run.result,
        "error": run.error,
        "elapsed_ms": run.elapsed_ms,
        "origin": run.origin,
        "is_current_text": run.text_sha256 == text_sha256(current_text),
        "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
