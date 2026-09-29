"""Durable one-post taxonomy queue. Never stores or logs source text in jobs."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PipelineEntry, TaxonomyClassification, TelegramChat, TelegramPost
from app.taxonomy.inference import MODEL_VERSION


def text_sha256(text: str | None) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def public_job(job: TaxonomyClassification | None, current_text: str | None = None) -> dict | None:
    if job is None:
        return None
    status = "stale" if current_text is not None and job.text_sha256 != text_sha256(current_text) else job.status
    return {
        "status": status,
        "model_version": job.model_version,
        "result": job.result if status in {"complete", "media_only"} else None,
        "error": job.error if status == "failed" else None,
        "elapsed_ms": job.elapsed_ms if status in {"complete", "media_only"} else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


async def enqueue(session: AsyncSession, entry_id: int) -> TaxonomyClassification:
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

    job = (
        await session.execute(
            select(TaxonomyClassification)
            .where(TaxonomyClassification.pipeline_entry_id == entry_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    fingerprint = text_sha256(post.text)
    if job is not None and job.status in {"queued", "running"} and job.text_sha256 == fingerprint:
        return job

    now = datetime.now(timezone.utc)
    has_text = bool((post.text or "").strip())
    if job is None:
        job = TaxonomyClassification(
            pipeline_entry_id=entry.id, source_post_id=post.id, text_sha256=fingerprint,
        )
        session.add(job)
    job.text_sha256 = fingerprint
    job.model_version = MODEL_VERSION if has_text else None
    job.status = "queued" if has_text else "media_only"
    job.result = None if has_text else {
        "top_3": [], "features": [], "technical_complexity": None,
        "review_status": "no_text", "score_kind": "no_model_inference",
        "media": bool(post.media_type or post.media_path),
    }
    job.error = None
    job.elapsed_ms = None if has_text else 0
    job.started_at = None
    job.finished_at = None if has_text else now
    job.updated_at = now
    if not has_text:
        entry.stage = "sorted"
        entry.status = "taxonomy_no_text"
        entry.last_operation_at = now
    elif entry.stage == "sorted":
        entry.stage = "received"
        entry.status = "received"
        entry.last_operation_at = now
    await session.flush()
    return job


async def invalidate_if_edited(session: AsyncSession, post_id: int, text: str | None) -> bool:
    job = (
        await session.execute(
            select(TaxonomyClassification)
            .where(TaxonomyClassification.source_post_id == post_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if job is None or job.text_sha256 == text_sha256(text):
        return False
    job.status = "stale"
    job.result = None
    job.error = None
    job.updated_at = datetime.now(timezone.utc)
    entry = (
        await session.execute(select(PipelineEntry).where(PipelineEntry.id == job.pipeline_entry_id))
    ).scalar_one_or_none()
    if entry is not None and entry.stage == "sorted" and entry.classification_id is None:
        entry.stage = "received"
        entry.status = "received"
        entry.last_operation_at = job.updated_at
    return True
