"""Одна маркировка для API и автоматического шага; публикации здесь нет."""

from datetime import datetime, timezone
from sqlalchemy import select
from fastapi import HTTPException
from app.models import TelegramPost, TaxonomyClassification
from app.taxonomy.jobs import text_sha256
from app.content.source_marking import marked_post_text, telegram_post_source_url
from app.content.selection_filters import has_marks
from app.web.source_media import album_primary, downloaded_media_path


async def album_posts(session, post):
    if post.grouped_id is None:
        return [post]
    return list(
        (
            await session.execute(
                select(TelegramPost)
                .where(
                    TelegramPost.chat_peer_id == post.chat_peer_id,
                    TelegramPost.grouped_id == post.grouped_id,
                    TelegramPost.is_deleted.is_(False),
                )
                .order_by(TelegramPost.message_id)
            )
        ).scalars()
    )


async def mark_source(session, entry, post, chat, manual=False):
    if post.is_deleted or entry.stage not in {"filtered", "marking", "ready"}:
        raise HTTPException(409, "Сначала отберите пост фильтром")
    jobs = list(
        (
            await session.execute(
                select(TaxonomyClassification).where(
                    TaxonomyClassification.pipeline_entry_id == entry.id
                )
            )
        ).scalars()
    )
    if any(j.status in {"ocr", "queued", "loading", "running"} for j in jobs):
        raise HTTPException(409, "Дождитесь завершения сортировки")
    from app.config import get_settings
    from app.ocr.jobs import classification_input_current
    valid = []
    for job in jobs:
        if job.status in {"complete", "media_only"} and await classification_input_current(session, post, job, get_settings()):
            from app.taxonomy.profiles import source_of
            if source_of(job) == "ocr" or job.text_sha256 == text_sha256(post.text):
                valid.append(job)
    if not valid:
        raise HTTPException(409, "Сначала обновите классификацию изменённого текста или OCR")
    if entry.stage == "filtered" and not await has_marks(session, entry.id):
        raise HTTPException(409, "У поста нет признаков отбора")
    primary = album_primary(await album_posts(session, post))
    url = telegram_post_source_url(primary, chat)
    if not url:
        raise HTTPException(409, "У этого чата нет ссылки на сообщение Telegram")
    entry.marked_text = marked_post_text(post.text, url)
    entry.marked_source_url = url
    entry.marked_text_sha256 = text_sha256(post.text)
    if entry.marked_at is None:
        entry.marked_at = datetime.now(timezone.utc)
    entry.last_operation_at = datetime.now(timezone.utc)
    if entry.stage != "ready":
        entry.stage, entry.status = "marking", "marked"
    if manual:
        entry.auto_manual_mark = True
    entry.auto_enabled = True
    entry.auto_state = "pending" if entry.stage != "ready" else "done"
    entry.last_error = None


async def readiness_error(session, entry, post, media_dir):
    if post.is_deleted:
        return "Исходное сообщение удалено"
    if not entry.marked_source_url or entry.marked_text_sha256 != text_sha256(
        post.text
    ):
        return "Маркировка устарела или отсутствует"
    # Даём коротким альбомным событиям собраться; это защита от неполной группы,
    # а не искусственная задержка стадий.
    posts = await album_posts(session, post)
    if post.grouped_id and (await session.execute(select(TelegramPost.id).where(
        TelegramPost.chat_peer_id == post.chat_peer_id,
        TelegramPost.grouped_id == post.grouped_id,
        TelegramPost.is_deleted.is_(True)).limit(1))).scalar_one_or_none():
        return "В альбоме удалено сообщение; нужна ручная проверка состава"
    if post.grouped_id and any(
        (datetime.now(timezone.utc) - p.updated_at).total_seconds() < 5 for p in posts
    ):
        return "Альбом ещё собирается"
    from app.config import get_settings
    from app.ocr.jobs import classification_input_current
    from app.taxonomy.profiles import source_of
    jobs = list((await session.execute(select(TaxonomyClassification).where(
        TaxonomyClassification.pipeline_entry_id == entry.id))).scalars())
    if jobs:
        valid = False
        for job in jobs:
            if job.status in {"complete", "media_only"} and (source_of(job) == "ocr" or job.text_sha256 == text_sha256(post.text)):
                valid = valid or await classification_input_current(session, post, job, get_settings())
        if not valid:
            return "OCR или медиа изменились; обновите классификацию"
    for item in posts:
        if item.media_type and downloaded_media_path(item, media_dir) is None:
            return f"Медиа сообщения {item.message_id} недоступно: {item.media_download_status}"
    return None
