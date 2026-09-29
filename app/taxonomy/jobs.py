"""One durable classification row per model and post; source text stays in TelegramPost."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PipelineEntry, TaxonomyClassification, TelegramChat, TelegramPost

MODEL_VERSIONS = {"tfidf": "codex-tfidf-3000-20260929", "minilm": "codex-minilm-3000-20260929-e2"}


def text_sha256(text: str | None) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def public_job(job: TaxonomyClassification | None, current_text: str | None = None) -> dict | None:
    if job is None:
        return None
    status = "stale" if current_text is not None and job.text_sha256 != text_sha256(current_text) else job.status
    return {
        "model_key": job.model_key,
        "status": status,
        "model_version": job.model_version,
        "result": job.result if status in {"complete", "media_only", "empty"} else None,
        "error": job.error if status == "failed" else None,
        "elapsed_ms": job.elapsed_ms if status in {"complete", "media_only", "empty"} else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


async def mark_without_text(session: AsyncSession, entry: PipelineEntry, post: TelegramPost) -> TaxonomyClassification | None:
    """Immediately sort textless posts; semantic workers never see them."""
    if (post.text or "").strip():
        return None
    fingerprint = text_sha256(post.text)
    job = (
        await session.execute(
            select(TaxonomyClassification)
            .where(TaxonomyClassification.pipeline_entry_id == entry.id, TaxonomyClassification.model_key == "media")
            .with_for_update()
        )
    ).scalar_one_or_none()
    has_media = bool(post.media_type or post.media_path)
    status = "media_only" if has_media else "empty"
    if job is None:
        job = TaxonomyClassification(
            pipeline_entry_id=entry.id, source_post_id=post.id, model_key="media", text_sha256=fingerprint
        )
        session.add(job)
    if (job.status != status or job.text_sha256 != fingerprint or entry.stage != "sorted"
            or (job.result or {}).get("category") != ("only_media" if has_media else "empty")):
        now = datetime.now(timezone.utc)
        job.text_sha256 = fingerprint
        job.model_version = None
        job.status = status
        job.result = {"category": "only_media" if has_media else "empty", "top_3": [], "features": [],
                      "technical_complexity": None, "review_status": "no_text", "score_kind": "no_model_inference",
                      "media": has_media}
        job.error = None
        job.elapsed_ms = 0
        job.started_at = None
        job.finished_at = now
        job.updated_at = now
        entry.stage = "sorted"
        entry.status = "taxonomy_media_only" if has_media else "taxonomy_empty"
        entry.last_operation_at = now
    await session.flush()
    return job


async def sort_textless_post(session: AsyncSession, post_id: int) -> TaxonomyClassification | None:
    row = (await session.execute(
        select(PipelineEntry, TelegramPost)
        .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
        .where(TelegramPost.id == post_id)
        .with_for_update(of=PipelineEntry)
    )).first()
    return await mark_without_text(session, *row) if row else None


async def enqueue(session: AsyncSession, entry_id: int, model_key: str) -> TaxonomyClassification:
    if model_key not in MODEL_VERSIONS:
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
    if chat.folder_name != "MAX" or post.is_deleted or entry.stage not in {"received", "sorted"}:
        raise HTTPException(409, detail="Карточка недоступна для сортировки")
    if not (post.text or "").strip():
        return await mark_without_text(session, entry, post)

    job = (
        await session.execute(
            select(TaxonomyClassification)
            .where(TaxonomyClassification.pipeline_entry_id == entry_id, TaxonomyClassification.model_key == model_key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    fingerprint = text_sha256(post.text)
    if job is not None and job.status in {"queued", "running"} and job.text_sha256 == fingerprint:
        return job
    now = datetime.now(timezone.utc)
    if job is None:
        job = TaxonomyClassification(
            pipeline_entry_id=entry.id, source_post_id=post.id, model_key=model_key, text_sha256=fingerprint
        )
        session.add(job)
    job.text_sha256 = fingerprint
    job.model_version = MODEL_VERSIONS[model_key]
    job.status = "queued"
    job.result = None
    job.error = None
    job.elapsed_ms = None
    job.started_at = None
    job.finished_at = None
    job.updated_at = now
    entry.last_operation_at = now
    await session.flush()
    return job


async def invalidate_if_edited(session: AsyncSession, post_id: int, text: str | None) -> bool:
    jobs = list((await session.execute(
        select(TaxonomyClassification).where(TaxonomyClassification.source_post_id == post_id).with_for_update()
    )).scalars())
    changed = [job for job in jobs if job.text_sha256 != text_sha256(text)]
    if not changed:
        return False
    now = datetime.now(timezone.utc)
    for job in changed:
        job.status = "stale"
        job.result = None
        job.error = None
        job.updated_at = now
    entry = (await session.execute(select(PipelineEntry).where(PipelineEntry.id == jobs[0].pipeline_entry_id))).scalar_one_or_none()
    if entry is not None and entry.stage == "sorted" and entry.classification_id is None:
        entry.stage = "received"
        entry.status = "received"
        entry.last_operation_at = now
    return True
