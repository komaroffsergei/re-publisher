from __future__ import annotations

import asyncio
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import typer
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.link_materials import link_summary_gate, load_link_materials
from app.content.pipeline_activity import try_acquire_pipeline_work_lock
from app.content.pipeline_entries import PIPELINE_STAGE_PUBLISHED, PIPELINE_STAGE_READY, sync_pipeline_entry_stage
from app.content.pipeline_logic import READY_DRAFT_STATUS
from app.content.state import mark_state
from app.content.text_utils import clean_text
from app.main import safe_echo
from app.models import ContentItem, LinkSnapshot, MediaAsset, PipelineEntry, PostLink, PublicationDraft, PublicationTarget, PublishedPost, Showcase

app = typer.Typer(no_args_is_help=True)
MAX_TEXT_LIMIT = 4000
NON_BLOCKING_RISK_FLAGS = {"missing_source_url", "missing_source_summary", "json_salvaged", "max_text_trimmed"}
BLOCKING_RISK_FLAGS = {"media_only_or_empty", "thin_source"}
MISSING_MEDIA_STATUS = "missing_media"
PUBLISH_FAILED_STATUS = "publish_failed"
PUBLISH_FAILED_MEDIA_STATUS = "publish_failed_media"
PUBLISH_FAILED_MEDIA_VERIFICATION_STATUS = "publish_failed_media_verification"
ATTACHMENT_NOT_READY_CODE = "attachment.not.ready"
IMAGE_MIME_PREFIX = "image/"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".webp", ".heic"}


class MaxPublisherError(RuntimeError):
    pass


@app.callback()
def main() -> None:
    """MAX publishing commands for the publication pipeline."""


def require_max_settings(settings: Settings) -> tuple[str, str, str]:
    if not settings.max_bot_token:
        raise MaxPublisherError("MAX_BOT_TOKEN is required.")
    if not settings.max_channel_chat_id:
        raise MaxPublisherError("MAX_CHANNEL_CHAT_ID is required.")
    return settings.max_bot_token, settings.max_channel_chat_id, settings.max_api_base.rstrip("/")


async def max_request(settings: Settings, method: str, path: str, **kwargs) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:
        raise MaxPublisherError(f"httpx unavailable: {exc}") from exc

    token, _chat_id, base = require_max_settings(settings)
    headers = kwargs.pop("headers", {})
    headers.update({"Authorization": token, "Accept": "application/json"})
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.request(method, f"{base}{path}", headers=headers, **kwargs)
        if response.status_code >= 400:
            body = clean_text(response.text)[:500]
            raise MaxPublisherError(f"MAX HTTP {response.status_code}: {body}")
        return response.json() if response.content else {}
    except Exception as exc:
        if isinstance(exc, MaxPublisherError):
            raise
        raise MaxPublisherError(f"MAX request failed: {exc}") from exc


async def check_credentials() -> dict[str, Any]:
    settings = settings_or_exit()
    _token, chat_id, _base = require_max_settings(settings)
    me = await max_request(settings, "GET", "/me")
    chat = await max_request(settings, "GET", f"/chats/{chat_id}")
    member = await max_request(settings, "GET", f"/chats/{chat_id}/members/me")
    permissions = member.get("permissions") if isinstance(member.get("permissions"), list) else []
    if not member.get("is_admin") or "write" not in permissions:
        raise MaxPublisherError("Bot must be channel admin with write permission.")
    return {
        "status": "ok",
        "bot": me.get("username") or me.get("user_id"),
        "chat_id": chat.get("chat_id"),
        "title": chat.get("title"),
        "permissions": permissions,
    }


def max_text_size(text: str) -> int:
    return len((text or "").encode("utf-16-le")) // 2


def trim_max_text(text: str, limit: int = MAX_TEXT_LIMIT) -> str:
    value = clean_text(text)
    if max_text_size(value) <= limit:
        return value
    low = 0
    high = len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if max_text_size(value[:mid]) <= limit:
            low = mid
        else:
            high = mid - 1
    clipped = value[:low].rstrip()
    word_clipped = clipped.rsplit(" ", 1)[0].strip() if " " in clipped else clipped
    if word_clipped and max_text_size(word_clipped) <= limit:
        clipped = word_clipped
    while clipped and max_text_size(clipped) > limit:
        clipped = clipped[:-1].rstrip()
    return clipped


def compose_max_text(draft: PublicationDraft) -> str:
    title = clean_text(draft.title)
    body = clean_text(draft.body)
    text = f"**{title}**\n\n{body}" if title else body
    return trim_max_text(text, MAX_TEXT_LIMIT)


def has_blocking_risk(risk_flags: list[str] | None) -> bool:
    return any(flag in BLOCKING_RISK_FLAGS or flag not in NON_BLOCKING_RISK_FLAGS for flag in (risk_flags or []))


def media_path(media: MediaAsset) -> Path | None:
    if not media.local_path:
        return None
    path = Path(media.local_path)
    return path if path.is_absolute() else Path.cwd() / path


def media_publish_blocker(media: MediaAsset | None) -> str | None:
    if media is None:
        return MISSING_MEDIA_STATUS
    path = media_path(media)
    if path is None:
        return "media_local_path_missing"
    if not path.exists() or not path.is_file():
        return f"media_file_missing:{media.local_path}"
    mime_type = clean_text(media.mime_type).lower()
    suffix = path.suffix.lower()
    if mime_type and not mime_type.startswith(IMAGE_MIME_PREFIX):
        return f"media_not_image:{media.mime_type}"
    if not mime_type and suffix not in IMAGE_SUFFIXES:
        return f"media_not_image:{suffix or 'unknown'}"
    return None


TOKEN_FIELD_HINTS = ("token", "attachment", "file_token", "filetoken", "media_token", "upload_token")


def looks_like_upload_token(value: Any) -> bool:
    text = clean_text(value)
    if len(text) < 16 or len(text) > 512:
        return False
    if "/" in text or "://" in text or " " in text:
        return False
    return True


def extract_upload_token(payload: Any, upload_url: str | None = None, *, parent_key: str = "") -> str | None:
    key_hint = parent_key.lower()
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = clean_text(key).lower()
            if any(hint in key_text for hint in TOKEN_FIELD_HINTS) and not isinstance(value, (dict, list)):
                token = clean_text(value)
                if token:
                    return token
            token = extract_upload_token(value, parent_key=key_text)
            if token:
                return token
    elif isinstance(payload, list):
        for value in payload:
            token = extract_upload_token(value, parent_key=parent_key)
            if token:
                return token
    elif any(hint in key_hint for hint in TOKEN_FIELD_HINTS) and looks_like_upload_token(payload):
        return clean_text(payload)
    if upload_url:
        query = parse_qs(urlparse(upload_url).query)
        for key, values in query.items():
            key_text = clean_text(key).lower()
            if values and any(hint in key_text for hint in TOKEN_FIELD_HINTS):
                return clean_text(values[0])
    return None


def upload_token_debug(upload_meta: Any, upload_payload: Any, upload_url: str) -> str:
    def keys(value: Any) -> list[str]:
        return sorted(str(key) for key in value.keys()) if isinstance(value, dict) else [type(value).__name__]

    query_keys = sorted(parse_qs(urlparse(upload_url).query).keys())
    return (
        f"MAX media upload token missing: "
        f"upload_meta_keys={keys(upload_meta)} "
        f"upload_payload_keys={keys(upload_payload)} "
        f"upload_url_query_keys={query_keys}"
    )[:800]


def message_payload(data: dict[str, Any]) -> dict[str, Any]:
    message = data.get("message") if isinstance(data.get("message"), dict) else data
    return message if isinstance(message, dict) else {}


def message_id_from_payload(message: dict[str, Any]) -> str | None:
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    message_id = body.get("mid") or message.get("message_id") or message.get("id")
    return clean_text(message_id) or None


def message_has_image_attachment(message: dict[str, Any]) -> bool:
    containers = [message]
    body = message.get("body")
    if isinstance(body, dict):
        containers.append(body)
    for container in containers:
        attachments = container.get("attachments")
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            attachment_type = clean_text(attachment.get("type")).lower()
            if attachment_type in {"image", "photo"}:
                return True
    return False


async def upload_max_image(settings: Settings, media: MediaAsset) -> dict[str, str]:
    blocker = media_publish_blocker(media)
    if blocker:
        raise MaxPublisherError(blocker)
    token, _chat_id, _base = require_max_settings(settings)
    path = media_path(media)
    if path is None:
        raise MaxPublisherError("media_local_path_missing")
    upload_meta = await max_request(settings, "POST", "/uploads", params={"type": "image"})
    upload_url = clean_text(upload_meta.get("url"))
    if not upload_url:
        raise MaxPublisherError("MAX upload URL missing")
    try:
        import httpx
    except ImportError as exc:
        raise MaxPublisherError(f"httpx unavailable: {exc}") from exc

    headers = {"Authorization": token, "Accept": "application/json"}
    mime_type = clean_text(media.mime_type) or "application/octet-stream"
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            with path.open("rb") as handle:
                response = await client.post(
                    upload_url,
                    headers=headers,
                    files={"data": (path.name, handle, mime_type)},
                )
        if response.status_code >= 400:
            raise MaxPublisherError(f"MAX media upload HTTP {response.status_code}: {clean_text(response.text)[:500]}")
        upload_payload = response.json() if response.content else {}
    except Exception as exc:
        if isinstance(exc, MaxPublisherError):
            raise
        raise MaxPublisherError(f"MAX media upload failed: {exc}") from exc
    uploaded_token = extract_upload_token(upload_payload, upload_url) or extract_upload_token(upload_meta, upload_url)
    if not uploaded_token:
        raise MaxPublisherError(upload_token_debug(upload_meta, upload_payload, upload_url))
    return {"token": uploaded_token}


async def verify_max_message_media(settings: Settings, message_id: str, *, attempts: int = 3) -> tuple[bool, dict[str, Any]]:
    last_message: dict[str, Any] = {}
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(min(2**attempt, 8))
        data = await max_request(settings, "GET", f"/messages/{message_id}")
        last_message = message_payload(data)
        if message_has_image_attachment(last_message):
            return True, last_message
    return False, last_message


async def select_publish_media(session: AsyncSession, post_id: int, draft: PublicationDraft | None = None) -> MediaAsset | None:
    candidates: list[MediaAsset] = []
    candidate_ids: list[int] = []
    if draft and draft.image_asset_id:
        candidate_ids.append(int(draft.image_asset_id))
    item = (
        await session.execute(select(ContentItem).where(ContentItem.source_post_id == post_id).limit(1))
    ).scalar_one_or_none()
    if item and item.primary_image_asset_id:
        candidate_ids.append(int(item.primary_image_asset_id))
    seen_ids: set[int] = set()
    for asset_id in candidate_ids:
        if asset_id in seen_ids:
            continue
        seen_ids.add(asset_id)
        media = (
            await session.execute(select(MediaAsset).where(MediaAsset.id == asset_id, MediaAsset.download_status == "done"))
        ).scalar_one_or_none()
        if media:
            candidates.append(media)
    candidates.extend(
        list(
            (
                await session.execute(
                    select(MediaAsset)
                    .where(MediaAsset.source_post_id == post_id, MediaAsset.download_status == "done")
                    .order_by(MediaAsset.source_type.desc(), MediaAsset.id)
                )
            ).scalars()
        )
    )
    candidates.extend(
        list(
            (
                await session.execute(
                    select(MediaAsset)
                    .join(LinkSnapshot, LinkSnapshot.image_asset_id == MediaAsset.id)
                    .join(PostLink, PostLink.id == LinkSnapshot.link_id)
                    .where(PostLink.post_id == post_id, MediaAsset.download_status == "done")
                    .order_by(PostLink.position_index.nulls_last(), MediaAsset.id)
                )
            ).scalars()
        )
    )
    seen: set[int] = set()
    for media in candidates:
        if int(media.id) in seen:
            continue
        seen.add(int(media.id))
        if media_publish_blocker(media) is None:
            return media
    return None


async def send_max_message(settings: Settings, draft: PublicationDraft, media: MediaAsset | None = None) -> tuple[str | None, str | None]:
    _token, chat_id, _base = require_max_settings(settings)
    if media is None:
        raise MaxPublisherError(MISSING_MEDIA_STATUS)
    attachment_payload = await upload_max_image(settings, media)
    payload = {
        "text": compose_max_text(draft),
        "attachments": [{"type": "image", "payload": attachment_payload}],
        "format": "markdown",
        "notify": True,
    }
    data: dict[str, Any] | None = None
    for attempt in range(4):
        try:
            data = await max_request(settings, "POST", "/messages", params={"chat_id": chat_id}, json=payload)
            break
        except MaxPublisherError as exc:
            if ATTACHMENT_NOT_READY_CODE in str(exc) and attempt < 3:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            raise
    message = message_payload(data or {})
    message_id = message_id_from_payload(message)
    if not message_id:
        raise MaxPublisherError("MAX message id missing after publish")
    verified, verified_message = await verify_max_message_media(settings, message_id)
    if not verified:
        raise MaxPublisherError(PUBLISH_FAILED_MEDIA_VERIFICATION_STATUS)
    message = verified_message or message
    url = clean_text(message.get("url") if isinstance(message, dict) else None)
    return message_id or None, url or None


async def promote_clean_review(limit: int | None = None) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        stmt = (
            select(PipelineEntry, PublicationDraft)
            .join(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
            .where(
                PipelineEntry.is_eligible.is_(True),
                PipelineEntry.publication_allowed.is_(True),
                PipelineEntry.published_post_id.is_(None),
                PipelineEntry.status == "needs_review",
                PublicationDraft.status == "needs_review",
                PublicationDraft.validation_errors == [],
            )
            .order_by(PipelineEntry.id)
        )
        if limit:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).all())
        for entry, draft in rows:
            if has_blocking_risk(draft.risk_flags):
                continue
            gate = link_summary_gate(await load_link_materials(session, entry.source_post_id))
            if not gate.ok:
                await session.execute(
                    update(PipelineEntry)
                    .where(PipelineEntry.id == entry.id)
                    .values(status=gate.status, last_error=gate.reason, updated_at=datetime.now(timezone.utc))
                )
                await mark_state(session, entry.source_post_id, rewrite_status=gate.status, last_error=gate.reason)
                continue
            await session.execute(update(PublicationDraft).where(PublicationDraft.id == draft.id).values(status=READY_DRAFT_STATUS, updated_at=datetime.now(timezone.utc)))
            await session.execute(update(PipelineEntry).where(PipelineEntry.id == entry.id).values(status=READY_DRAFT_STATUS, last_error=None, updated_at=datetime.now(timezone.utc)))
            count += 1
        await session.commit()
    return count


async def schedule_ready(*, refresh: bool = False, limit: int | None = None) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    min_delta = settings.max_publish_random_min_minutes
    max_delta = max(settings.max_publish_random_max_minutes, min_delta)
    count = 0
    async with factory() as session:
        stmt = (
            select(PipelineEntry, PublicationDraft)
            .join(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
            .outerjoin(PublishedPost, PublishedPost.id == PipelineEntry.published_post_id)
            .where(
                PipelineEntry.is_eligible.is_(True),
                PipelineEntry.publication_allowed.is_(True),
                PipelineEntry.status == READY_DRAFT_STATUS,
                PublicationDraft.status == READY_DRAFT_STATUS,
                PublicationDraft.validation_errors == [],
                PublishedPost.id.is_(None),
            )
            .order_by(PipelineEntry.scheduled_publish_at.nulls_last(), PipelineEntry.id)
        )
        if not refresh:
            stmt = stmt.where(PipelineEntry.scheduled_publish_at.is_(None))
        if limit:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).all())
        scheduled_at = datetime.now(timezone.utc)
        for entry, _draft in rows:
            scheduled_at = scheduled_at + timedelta(minutes=random.randint(min_delta, max_delta))
            await session.execute(
                update(PipelineEntry)
                .where(PipelineEntry.id == entry.id)
                .values(scheduled_publish_at=scheduled_at, updated_at=datetime.now(timezone.utc))
            )
            count += 1
        await session.commit()
    return count


def publish_status_from_error(error: str | None) -> str:
    text = (error or "").lower()
    if MISSING_MEDIA_STATUS in text:
        return MISSING_MEDIA_STATUS
    if PUBLISH_FAILED_MEDIA_VERIFICATION_STATUS in text:
        return PUBLISH_FAILED_MEDIA_VERIFICATION_STATUS
    if "media" in text or "upload" in text:
        return PUBLISH_FAILED_MEDIA_STATUS
    return PUBLISH_FAILED_STATUS


async def publish_ready_entry(
    session: AsyncSession,
    settings: Settings,
    entry: PipelineEntry,
    draft: PublicationDraft,
    showcase: Showcase,
) -> dict[str, Any]:
    await mark_state(session, entry.source_post_id, publication_status="running", last_error=None)
    await session.commit()
    gate = link_summary_gate(await load_link_materials(session, entry.source_post_id))
    if not gate.ok:
        await session.execute(
            update(PipelineEntry)
            .where(PipelineEntry.id == entry.id)
            .values(status=gate.status, last_error=gate.reason, updated_at=datetime.now(timezone.utc))
        )
        await mark_state(session, entry.source_post_id, publication_status=gate.status, last_error=gate.reason)
        await sync_pipeline_entry_stage(session, entry.source_post_id)
        return {"attempted": 0, "published": 0, "failed": 1, "status": gate.status, "error": gate.reason}

    selected_media = await select_publish_media(session, entry.source_post_id, draft)
    media_error = media_publish_blocker(selected_media)
    if media_error:
        error = media_error[:800]
        await session.execute(
            update(PipelineEntry)
            .where(PipelineEntry.id == entry.id)
            .values(
                status=MISSING_MEDIA_STATUS,
                last_error=error,
                updated_at=datetime.now(timezone.utc),
                last_operation_at=datetime.now(timezone.utc),
            )
        )
        await mark_state(session, entry.source_post_id, publication_status=MISSING_MEDIA_STATUS, last_error=error)
        await sync_pipeline_entry_stage(session, entry.source_post_id)
        return {"attempted": 0, "published": 0, "failed": 1, "status": MISSING_MEDIA_STATUS, "error": error}

    if selected_media and selected_media.id != draft.image_asset_id:
        await session.execute(
            update(PublicationDraft)
            .where(PublicationDraft.id == draft.id)
            .values(image_asset_id=selected_media.id, updated_at=datetime.now(timezone.utc))
        )

    status = "published"
    error = None
    message_id = published_url = None
    try:
        message_id, published_url = await send_max_message(settings, draft, selected_media)
    except Exception as exc:
        error = str(exc)[:800]
        status = publish_status_from_error(error)

    values = {
        "draft_id": draft.id,
        "showcase_id": showcase.id,
        "target_type": "max",
        "target_chat_id": settings.max_channel_chat_id,
        "target_message_id": message_id,
        "published_url": published_url,
        "status": "published" if status == "published" else "failed",
        "error": error,
        "published_at": datetime.now(timezone.utc) if status == "published" else None,
        "created_at": datetime.now(timezone.utc),
    }
    stmt_insert = insert(PublishedPost.__table__).values(**values)
    result = await session.execute(
        stmt_insert.on_conflict_do_update(
            constraint="uq_published_posts_draft_showcase",
            set_={key: stmt_insert.excluded[key] for key in values if key != "created_at"},
        ).returning(PublishedPost.id)
    )
    published_row_id = result.scalar_one()
    await session.execute(
        update(PipelineEntry)
        .where(PipelineEntry.id == entry.id)
        .values(
            published_post_id=published_row_id if status == "published" else entry.published_post_id,
            stage=PIPELINE_STAGE_PUBLISHED if status == "published" else PIPELINE_STAGE_READY,
            status="published" if status == "published" else status,
            last_error=error,
            updated_at=datetime.now(timezone.utc),
            last_operation_at=datetime.now(timezone.utc),
        )
    )
    await mark_state(session, entry.source_post_id, publication_status=status, last_error=error)
    await sync_pipeline_entry_stage(session, entry.source_post_id)
    return {
        "attempted": 1,
        "published": 1 if status == "published" else 0,
        "failed": 0 if status == "published" else 1,
        "status": status,
        "error": error,
        "message_id": message_id,
        "published_url": published_url,
        "media_asset_id": selected_media.id if selected_media else None,
    }


async def publish_due(limit: int) -> dict[str, int]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    now = datetime.now(timezone.utc)
    attempted = published = failed = busy = 0
    actual_limit = 1
    async with factory() as session:
        stmt = (
            select(PipelineEntry, PublicationDraft, PublicationTarget, Showcase, MediaAsset)
            .join(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
            .join(PublicationTarget, PublicationTarget.id == PublicationDraft.publication_target_id)
            .join(Showcase, Showcase.id == PublicationTarget.showcase_id)
            .outerjoin(MediaAsset, MediaAsset.id == PublicationDraft.image_asset_id)
            .outerjoin(PublishedPost, PublishedPost.id == PipelineEntry.published_post_id)
            .where(
                PipelineEntry.is_eligible.is_(True),
                PipelineEntry.publication_allowed.is_(True),
                PipelineEntry.status == READY_DRAFT_STATUS,
                PipelineEntry.scheduled_publish_at.is_not(None),
                PipelineEntry.scheduled_publish_at <= now,
                PublicationDraft.status == READY_DRAFT_STATUS,
                PublicationDraft.validation_errors == [],
                PublishedPost.id.is_(None),
            )
            .order_by(PipelineEntry.scheduled_publish_at, PipelineEntry.id)
            .limit(actual_limit)
        )
        rows = list((await session.execute(stmt)).all())
        for entry, draft, _target, showcase, _media in rows:
            lock = await try_acquire_pipeline_work_lock(settings, owner="publisher", entry_id=entry.id)
            if lock is None:
                busy += 1
                break
            try:
                result = await publish_ready_entry(session, settings, entry, draft, showcase)
                attempted += int(result.get("attempted") or 0)
                published += int(result.get("published") or 0)
                failed += int(result.get("failed") or 0)
            finally:
                await lock.release()
        await session.commit()
    return {"attempted": attempted, "published": published, "failed": failed, "busy": busy}


async def status_snapshot() -> dict[str, Any]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        due = (
            await session.execute(
                select(func.count())
                .select_from(PipelineEntry)
                .join(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
                .outerjoin(PublishedPost, PublishedPost.id == PipelineEntry.published_post_id)
                .where(
                    PipelineEntry.status == READY_DRAFT_STATUS,
                    PublicationDraft.status == READY_DRAFT_STATUS,
                    PipelineEntry.scheduled_publish_at <= datetime.now(timezone.utc),
                    PublishedPost.id.is_(None),
                )
            )
        ).scalar_one()
        next_at = (
            await session.execute(
                select(func.min(PipelineEntry.scheduled_publish_at))
                .select_from(PipelineEntry)
                .join(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
                .outerjoin(PublishedPost, PublishedPost.id == PipelineEntry.published_post_id)
                .where(PipelineEntry.status == READY_DRAFT_STATUS, PublicationDraft.status == READY_DRAFT_STATUS, PublishedPost.id.is_(None))
            )
        ).scalar_one()
    return {"due": int(due or 0), "next_publish_at": next_at.isoformat() if next_at else None}


@app.command("check-credentials")
def check_credentials_command() -> None:
    safe_echo(check_credentials_result := run_async(check_credentials()))


@app.command("promote-clean-review")
def promote_clean_review_command(limit: int | None = typer.Option(None, "--limit", min=1)) -> None:
    safe_echo(f"promoted={run_async(promote_clean_review(limit))}")


@app.command("schedule-ready")
def schedule_ready_command(
    refresh: bool = typer.Option(False, "--refresh"),
    limit: int | None = typer.Option(None, "--limit", min=1),
) -> None:
    safe_echo(f"scheduled={run_async(schedule_ready(refresh=refresh, limit=limit))}")


@app.command("publish-due")
def publish_due_command(limit: int = limit_option(10)) -> None:
    safe_echo(run_async(publish_due(limit)))


@app.command("status")
def status_command() -> None:
    safe_echo(run_async(status_snapshot()))


@app.command("run-loop")
def run_loop_command(
    interval_seconds: int | None = typer.Option(None, "--interval-seconds", min=5),
    publish_limit: int = typer.Option(10, "--publish-limit", min=1),
    max_cycles: int | None = typer.Option(None, "--max-cycles", min=1),
) -> None:
    settings = settings_or_exit()
    interval = interval_seconds or settings.max_publish_loop_interval_seconds
    cycle = 0
    while True:
        cycle += 1
        promoted = run_async(promote_clean_review())
        scheduled = run_async(schedule_ready())
        published = run_async(publish_due(1))
        snapshot = run_async(status_snapshot())
        safe_echo(
            f"ts={datetime.now(timezone.utc).isoformat()} cycle={cycle} promoted={promoted} "
            f"scheduled={scheduled} published={published} snapshot={snapshot}"
        )
        sys.stdout.flush()
        if max_cycles is not None and cycle >= max_cycles:
            break
        time.sleep(interval)


if __name__ == "__main__":
    app()
