from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content.link_materials import (
    LINK_SUMMARY_FAILED_STATUS,
    LINK_SUMMARY_PENDING_STATUS,
    link_summary_gate,
    load_link_materials,
)
from app.content.pipeline_logic import READY_DRAFT_STATUS, axes_from_label_scores, education_eligibility, status_from_state
from app.models import (
    ContentItem,
    MediaAsset,
    PipelineEntry,
    PostClassification,
    PublicationDraft,
    PublishedPost,
    TelegramChat,
    TelegramPost,
)

PIPELINE_STAGE_RECEIVED = "received"
PIPELINE_STAGE_SORTED = "sorted"
PIPELINE_STAGE_ENRICHED = "enriched"
PIPELINE_STAGE_REWRITTEN = "rewritten"
PIPELINE_STAGE_READY = "ready"
PIPELINE_STAGE_PUBLISHED = "published"

PIPELINE_STAGES = [
    PIPELINE_STAGE_RECEIVED,
    PIPELINE_STAGE_SORTED,
    PIPELINE_STAGE_ENRICHED,
    PIPELINE_STAGE_REWRITTEN,
    PIPELINE_STAGE_READY,
    PIPELINE_STAGE_PUBLISHED,
]

PIPELINE_STAGE_LABELS = {
    PIPELINE_STAGE_RECEIVED: "Не готовы",
    PIPELINE_STAGE_SORTED: "Отсортирован",
    PIPELINE_STAGE_ENRICHED: "Обогащен",
    PIPELINE_STAGE_REWRITTEN: "Переписан",
    PIPELINE_STAGE_READY: "Готов к публикации",
    PIPELINE_STAGE_PUBLISHED: "Опубликован",
}

INCOMPLETE_ENTRY_STATUSES = {
    "blocked",
    LINK_SUMMARY_PENDING_STATUS,
    LINK_SUMMARY_FAILED_STATUS,
    "published_incomplete",
    "publish_context_missing",
    "publish_failed",
    "publish_failed_media",
    "publish_failed_media_verification",
    "missing_media",
}


def link_summary_status_from_missing(missing: tuple[str, ...]) -> str | None:
    if LINK_SUMMARY_FAILED_STATUS in missing:
        return LINK_SUMMARY_FAILED_STATUS
    if LINK_SUMMARY_PENDING_STATUS in missing:
        return LINK_SUMMARY_PENDING_STATUS
    return None


@dataclass(frozen=True)
class PublicationReadiness:
    ok: bool
    has_rewrite: bool
    draft_ready: bool
    has_enrichment: bool
    links_ready: bool
    all_links_loaded: bool
    has_image: bool
    missing: tuple[str, ...]


def pipeline_stage_for_state(
    *,
    has_classification: bool,
    has_content_item: bool,
    is_eligible: bool,
    draft_status: str | None,
    has_published_post: bool,
    is_publication_ready: bool = False,
    is_enriched: bool = False,
    is_failed_or_incomplete: bool = False,
) -> str:
    if is_failed_or_incomplete:
        return PIPELINE_STAGE_RECEIVED
    if is_publication_ready and has_published_post:
        return PIPELINE_STAGE_PUBLISHED
    if is_publication_ready and draft_status == READY_DRAFT_STATUS:
        return PIPELINE_STAGE_READY
    if draft_status:
        return PIPELINE_STAGE_REWRITTEN
    if has_classification and has_content_item and is_eligible and is_enriched:
        return PIPELINE_STAGE_ENRICHED
    if has_classification:
        return PIPELINE_STAGE_SORTED
    return PIPELINE_STAGE_RECEIVED


async def publication_readiness_for_post(
    session: AsyncSession,
    post_id: int,
    *,
    item: ContentItem | None,
    draft: PublicationDraft | None,
    is_eligible: bool,
    publication_allowed: bool,
) -> PublicationReadiness:
    materials = await load_link_materials(session, post_id)
    link_gate = link_summary_gate(materials)
    all_links_loaded = all(str(material.link.extraction_status or "").lower() == "done" for material in materials)
    media_asset_id = (
        await session.execute(
            select(MediaAsset.id)
            .where(MediaAsset.source_post_id == post_id, MediaAsset.download_status == "done")
            .limit(1)
        )
    ).scalar_one_or_none()
    has_image = bool(
        (draft and draft.image_asset_id)
        or (item and item.primary_image_asset_id)
        or media_asset_id
        or any(material.snapshot and material.snapshot.image_asset_id for material in materials)
    )
    has_rewrite = draft is not None
    draft_ready = bool(draft and draft.status == READY_DRAFT_STATUS)
    links_ready = bool(link_gate.ok and all_links_loaded)
    has_enrichment = bool(item and links_ready and has_image)
    missing: list[str] = []
    if not publication_allowed:
        missing.append("publication_blocked")
    if not is_eligible:
        missing.append("ineligible")
    if not item:
        missing.append("material")
    if not has_rewrite:
        missing.append("rewrite")
    elif not draft_ready:
        missing.append("ready_draft")
    if not all_links_loaded:
        missing.append("links_loaded")
    if not link_gate.ok:
        missing.append(link_gate.status or "link_summaries")
    if not has_image:
        missing.append("image")
    return PublicationReadiness(
        ok=bool(publication_allowed and is_eligible and item and draft_ready and has_enrichment and has_image),
        has_rewrite=has_rewrite,
        draft_ready=draft_ready,
        has_enrichment=has_enrichment,
        links_ready=links_ready,
        all_links_loaded=all_links_loaded,
        has_image=has_image,
        missing=tuple(missing),
    )


async def ensure_pipeline_entry_for_post(session: AsyncSession, post_id: int) -> int:
    now = datetime.now(timezone.utc)
    values = {
        "source_post_id": post_id,
        "content_item_id": None,
        "stage": PIPELINE_STAGE_RECEIVED,
        "status": PIPELINE_STAGE_RECEIVED,
        "publication_allowed": True,
        "is_eligible": False,
        "genre_secondary": [],
        "created_at": now,
        "updated_at": now,
        "last_operation_at": now,
    }
    table = PipelineEntry.__table__
    stmt = insert(table).values(**values)
    return int(
        (
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_pipeline_entries_source_post",
                    set_={"updated_at": now, "last_operation_at": now},
                ).returning(table.c.id)
            )
        ).scalar_one()
    )


async def ensure_missing_pipeline_entries(
    session: AsyncSession,
    *,
    folder_name: str,
    limit: int,
) -> int:
    rows = list(
        (
            await session.execute(
                select(TelegramPost.id)
                .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                .outerjoin(PipelineEntry, PipelineEntry.source_post_id == TelegramPost.id)
                .where(
                    TelegramPost.is_deleted.is_(False),
                    TelegramChat.folder_name == folder_name,
                    PipelineEntry.id.is_(None),
                )
                .order_by(TelegramPost.created_at.desc(), TelegramPost.id.desc())
                .limit(limit)
            )
        ).scalars()
    )
    for post_id in rows:
        await ensure_pipeline_entry_for_post(session, int(post_id))
    return len(rows)


async def latest_classification_for_item(session: AsyncSession, item: ContentItem | None) -> PostClassification | None:
    if not item:
        return None
    return (
        await session.execute(
            select(PostClassification)
            .where(PostClassification.content_item_id == item.id)
            .order_by(PostClassification.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def latest_draft_for_entry(session: AsyncSession, entry: PipelineEntry) -> PublicationDraft | None:
    if entry.latest_draft_id:
        draft = (
            await session.execute(select(PublicationDraft).where(PublicationDraft.id == entry.latest_draft_id))
        ).scalar_one_or_none()
        if draft:
            return draft
    return (
        await session.execute(
            select(PublicationDraft)
            .where(PublicationDraft.source_post_id == entry.source_post_id)
            .order_by(PublicationDraft.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def published_for_entry(session: AsyncSession, entry: PipelineEntry, draft: PublicationDraft | None) -> PublishedPost | None:
    if entry.published_post_id:
        published = (
            await session.execute(
                select(PublishedPost).where(PublishedPost.id == entry.published_post_id, PublishedPost.status == "published")
            )
        ).scalar_one_or_none()
        if published:
            return published
    if not draft:
        return None
    return (
        await session.execute(
            select(PublishedPost).where(PublishedPost.draft_id == draft.id, PublishedPost.status == "published").limit(1)
        )
    ).scalar_one_or_none()


async def sync_pipeline_entry_stage(session: AsyncSession, post_id: int) -> PipelineEntry | None:
    await ensure_pipeline_entry_for_post(session, post_id)
    entry = (
        await session.execute(select(PipelineEntry).where(PipelineEntry.source_post_id == post_id))
    ).scalar_one_or_none()
    if not entry:
        return None

    item = (
        await session.execute(select(ContentItem).where(ContentItem.source_post_id == post_id))
    ).scalar_one_or_none()
    classification = await latest_classification_for_item(session, item)
    draft = await latest_draft_for_entry(session, entry)
    published = await published_for_entry(session, entry, draft)
    axes: dict[str, int] = axes_from_label_scores(classification.label_scores if classification else None)
    eligibility = (
        education_eligibility(
            classification.label_primary,
            list(classification.label_secondary or []),
            axes,
            needs_review=bool(classification.needs_review),
        )
        if classification
        else None
    )
    is_eligible = bool(eligibility.is_eligible) if eligibility else False
    readiness = await publication_readiness_for_post(
        session,
        post_id,
        item=item,
        draft=draft,
        is_eligible=is_eligible,
        publication_allowed=bool(entry.publication_allowed),
    )
    link_summary_status = link_summary_status_from_missing(readiness.missing)
    if not entry.publication_allowed:
        status = "blocked"
    elif published is not None and not readiness.ok:
        status = "published_incomplete"
    elif readiness.ok and published is not None:
        status = "published"
    elif entry.status in INCOMPLETE_ENTRY_STATUSES and published is None:
        status = entry.status
    elif readiness.ok:
        status = READY_DRAFT_STATUS
    elif classification is None:
        status = PIPELINE_STAGE_RECEIVED
    elif not is_eligible:
        status = "ineligible"
    elif link_summary_status:
        status = link_summary_status
    elif draft is not None and draft.status == READY_DRAFT_STATUS:
        status = "not_ready"
    elif draft is not None:
        status = draft.status
    else:
        status = status_from_state(
            publication_allowed=entry.publication_allowed,
            is_eligible=is_eligible,
            draft_status=draft.status if draft else None,
            has_published_post=published is not None,
            has_error=bool(entry.last_error),
        )
        if (
            entry.status in {LINK_SUMMARY_PENDING_STATUS, LINK_SUMMARY_FAILED_STATUS, "rewrite_running"}
            and not draft
            and published is None
        ):
            status = entry.status
        if entry.status == "publish_failed" and draft and published is None:
            status = entry.status
    stage = pipeline_stage_for_state(
        has_classification=classification is not None,
        has_content_item=item is not None,
        is_eligible=is_eligible,
        draft_status=draft.status if draft else None,
        has_published_post=published is not None,
        is_publication_ready=readiness.ok,
        is_enriched=readiness.has_enrichment,
        is_failed_or_incomplete=status in INCOMPLETE_ENTRY_STATUSES,
    )
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "content_item_id": item.id if item else None,
        "classification_id": classification.id if classification else None,
        "classification_model_version": classification.classifier_version if classification else None,
        "genre_primary": classification.label_primary if classification else None,
        "genre_secondary": list(classification.label_secondary or []) if classification else [],
        "confidence": float(classification.confidence or 0) if classification else None,
        "difficulty_score": axes.get("difficulty_score") if classification else None,
        "promo_score": axes.get("promo_score") if classification else None,
        "opinion_score": axes.get("opinion_score") if classification else None,
        "event_score": axes.get("event_score") if classification else None,
        "is_eligible": is_eligible,
        "eligibility_reason": eligibility.reason if eligibility else None,
        "latest_draft_id": draft.id if draft else None,
        "published_post_id": published.id if published else None,
        "stage": stage,
        "status": status,
        "updated_at": now,
        "last_operation_at": now,
    }
    await session.execute(update(PipelineEntry).where(PipelineEntry.id == entry.id).values(**values))
    return (
        await session.execute(select(PipelineEntry).where(PipelineEntry.id == entry.id))
    ).scalar_one_or_none()
