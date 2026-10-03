"""Подготовка входа, очередь и история OCR. Здесь нет тематических эвристик."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select
from fastapi import HTTPException
from app.models import PipelineEntry, TelegramPost
from app.ocr.models import OcrJob, OcrRun
from app.ocr.engine import compose_input, compose_ocr, media_digest, input_digest, engine_version
from app.ocr.media import safe_path

TERMINAL = {"complete", "no_text", "needs_review", "failed"}


async def snapshot(session, post, settings):
    # Импорт внутри функции устраняет зависимость taxonomy -> OCR -> taxonomy.
    from app.content.post_preparation import album_posts
    posts = await album_posts(session, post)
    inputs, provenance, error = [], [], None
    # Идентичное превью не делает заменённое видео прежним вложением.
    # В отпечатке учитываем все оригиналы, включая не используемые OCR.
    for item in posts:
        original = safe_path(item.media_path, settings.media_dir)
        raw_media = (item.raw or {}).get("media") or {}
        media_object = raw_media.get("photo") or raw_media.get("document") or {}
        provenance.append({"message_id": item.message_id, "type": item.media_type,
            "identity": media_object.get("id"), "status": item.media_download_status,
            "original_sha256": media_digest(original) if original else None})
    deleted = bool(post.grouped_id and (await session.execute(select(TelegramPost.id).where(
        TelegramPost.chat_peer_id == post.chat_peer_id,
        TelegramPost.grouped_id == post.grouped_id,
        TelegramPost.is_deleted.is_(True)).limit(1))).scalar_one_or_none())
    if deleted:
        error = "В альбоме удалено сообщение; нужен ручной разбор состава"
    now = datetime.now(timezone.utc)
    # Редактирование подписи не означает, что альбом снова собирается.
    if not deleted and post.grouped_id and any(p.created_at and (now - p.created_at).total_seconds() < 5 for p in posts):
        error = "Альбом ещё собирается"
    for item in posts:
        if not item.media_type:
            continue
        original = safe_path(item.media_path, settings.media_dir)
        preview = safe_path(item.ocr_preview_path, settings.ocr_preview_dir)
        kind = (item.media_type or "").lower()
        # Документы определяем по MIME, не расширению пользовательского имени.
        mime = ((item.raw or {}).get("media") or {}).get("document", {}).get("mime_type", "")
        video = "video" in kind or mime.startswith("video/")
        image = any(t in kind for t in ("photo", "image", "gif")) or mime.startswith("image/")
        if not video and not image:
            continue
        selected = preview if video and preview else original
        if selected is None:
            error = f"Медиа сообщения {item.message_id} недоступно: {item.media_download_status}"
        inputs.append({"post_id": item.id, "message_id": item.message_id,
            "kind": "video_frame" if video and not preview else "image",
            "path": str(selected) if selected else None,
            "sha256": media_digest(selected) if selected else None,
            "original_status": item.media_download_status,
            "preview_status": item.ocr_preview_status if video else None})
    data = {"inputs": inputs, "media": provenance, "error": error,
            "contract": "ocr_media_v2",
            "engine_version": engine_version(settings.ocr_model_dir) if settings.ocr_enabled else "disabled"}
    source_sha = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return source_sha, inputs, error


async def enqueue_ocr(session, entry, post, settings, retry=False):
    if not settings.ocr_enabled:
        raise HTTPException(409, "OCR-профиль пока выключен")
    if post.is_deleted:
        raise HTTPException(409, "Пост удалён")
    source, inputs, error = await snapshot(session, post, settings)
    job = (await session.execute(select(OcrJob).where(OcrJob.entry_id == entry.id).with_for_update())).scalar_one_or_none()
    if job and job.source_sha256 == source and (not retry or job.status in {"queued", "running"}):
        if job.status in {"complete", "no_text"} and job.current_run_id:
            from app.ocr.engine import needs_review
            current = await session.get(OcrRun, job.current_run_id)
            if current and any(needs_review(r) for r in current.results):
                # История прежнего запуска сохраняется, но новый запуск модели
                # не ждёт уже завершённый непригодный OCR бесконечно.
                job.status = "needs_review"
                job.error = "Есть строки с оценкой OCR ниже 50%; частичный текст не передаётся модели"
                job.updated_at = datetime.now(timezone.utc)
        return job
    if job and job.status == "running":
        raise HTTPException(409, "OCR уже выполняется")
    now = datetime.now(timezone.utc)
    if job and job.current_run_id:
        previous = await session.get(OcrRun, job.current_run_id)
        if previous and previous.status in {"queued", "waiting_media"}:
            previous.status, previous.finished_at = "stale", now
    if job is None:
        job = OcrJob(entry_id=entry.id, source_sha256=source, status="queued")
        session.add(job)
    # Старый результат остаётся в истории; текущая ссылка заменяется.
    run = OcrRun(entry_id=entry.id, source_sha256=source, status="waiting_media" if error else "queued",
                 inputs=inputs, results=[], error=error, total_inputs=len(inputs), completed_inputs=0)
    session.add(run)
    await session.flush()
    job.current_run_id, job.source_sha256 = run.id, source
    job.status, job.error = run.status, error
    job.attempts, job.retry_at, job.updated_at = 0, None, now
    return job


async def current_input(session, entry_id, post, settings, input_source="combined"):
    """Нет готового полного OCR — нет входа классификатора, даже при подписи."""
    job = await session.get(OcrJob, entry_id)
    if not job:
        return None, None
    source, _inputs, error = await snapshot(session, post, settings)
    run = await session.get(OcrRun, job.current_run_id) if job.current_run_id else None
    from app.ocr.engine import needs_review
    if error or not run or job.source_sha256 != source or job.status not in {"complete", "no_text"} or any(needs_review(r) for r in run.results):
        return None, run
    return (compose_ocr(run.results) if input_source == "ocr" else compose_input(post.text, run.results)), run


def classification_digest(post, run, input_source):
    return input_digest(None if input_source == "ocr" else post.text, run.results)


async def public_ocr(session, entry_id, post, settings):
    job = await session.get(OcrJob, entry_id)
    if not job:
        return {"status": "not_started", "runs": []}
    source, _inputs, _error = await snapshot(session, post, settings)
    rows = list((await session.execute(select(OcrRun).where(OcrRun.entry_id == entry_id)
                      .order_by(OcrRun.id.desc()).limit(20))).scalars())
    from app.ocr.engine import needs_review
    current = next((r for r in rows if r.id == job.current_run_id), None)
    status = job.status
    error = job.error
    if status in {"complete", "no_text"} and current and any(needs_review(r) for r in current.results):
        status = "needs_review"
        error = error or "Есть строки с оценкой OCR ниже 50%; частичный текст не передаётся модели"
    return {"status": status if job.source_sha256 == source else "stale", "error": error,
            "completed_inputs": current.completed_inputs if current else 0,
            "total_inputs": current.total_inputs if current else 0,
            "run_id": job.current_run_id, "runs": [{"id": r.id, "status": r.status,
            "engine_version": r.engine_version, "input_sha256": r.input_sha256,
            "results": r.results, "elapsed_ms": r.elapsed_ms, "error": r.error,
            "current": r.id == job.current_run_id and r.source_sha256 == source,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in rows]}


async def classification_input_current(session, post, job, settings):
    """Оценка подписи не подтверждает актуальность OCR другого профиля."""
    from app.taxonomy.profiles import source_of
    if not job or source_of(job) == "text":
        return True
    value, run = await current_input(session, job.pipeline_entry_id, post, settings, source_of(job))
    return value is not None and run is not None and classification_digest(post, run, source_of(job)) == job.input_sha256


async def invalidate_ocr(session, post, settings):
    """Изменение части альбома отзывает результаты и готовность всей группы."""
    from app.models import TaxonomyClassification, TaxonomyRun
    statement = select(PipelineEntry, TelegramPost).join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
    statement = statement.where(TelegramPost.chat_peer_id == post.chat_peer_id, TelegramPost.grouped_id == post.grouped_id) if post.grouped_id else statement.where(TelegramPost.id == post.id)
    for entry, item in (await session.execute(statement.with_for_update(of=PipelineEntry))).all():
        ocr_job = await session.get(OcrJob, entry.id)
        if not ocr_job:
            continue
        source, _inputs, _error = await snapshot(session, item, settings)
        if source == ocr_job.source_sha256:
            continue
        ocr_job.status = "stale"
        jobs = list((await session.execute(select(TaxonomyClassification).where(
            TaxonomyClassification.pipeline_entry_id == entry.id, TaxonomyClassification.input_source.in_(("ocr", "combined"))))).scalars())
        for job in jobs:
            job.status, job.error = "stale", None
            if job.current_run_id:
                run = await session.get(TaxonomyRun, job.current_run_id)
                if run and run.status in {"ocr", "queued", "loading", "running"}:
                    run.status, run.finished_at = "stale", datetime.now(timezone.utc)
        if entry.stage != "published":
            entry.ready_at = None
            entry.marked_text = entry.marked_source_url = entry.marked_text_sha256 = entry.marked_at = None
            if entry.stage in {"marking", "ready"}:
                entry.stage = "filtered"
            if entry.auto_enabled:
                entry.auto_state, entry.auto_retry_at = "pending", None
