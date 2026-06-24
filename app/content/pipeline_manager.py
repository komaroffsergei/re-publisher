from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import typer
from joblib import load
from sqlalchemy import func, or_, select, update
from sqlalchemy import case as sql_case
from sqlalchemy.dialects.postgresql import insert

from app.content.classifier import build_classification_text, predict
from app.content.codex_supervised_loop import is_media_only_unknown, media_only_prediction, predict_bundle
from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.link_enricher import enrich_pending, enrich_post_links
from app.content.link_materials import LINK_SUMMARY_FAILED_STATUS, LINK_SUMMARY_PENDING_STATUS
from app.content.local_summary import summarize_pending, summarize_post_links
from app.content.local_translation import translate_pending
from app.content.material_builder import build_new, build_post_material
from app.content.max_publisher import (
    MISSING_MEDIA_STATUS,
    PUBLISH_FAILED_STATUS,
    publish_ready_entry,
    select_publish_media,
)
from app.content.media_assets import download_link_images, download_link_images_for_post, register_telegram_media, register_telegram_media_for_post
from app.content.pipeline_activity import try_acquire_pipeline_work_lock
from app.content.pipeline_entries import (
    PIPELINE_STAGE_ENRICHED,
    PIPELINE_STAGE_PUBLISHED,
    PIPELINE_STAGE_READY,
    PIPELINE_STAGE_RECEIVED,
    PIPELINE_STAGE_REWRITTEN,
    PIPELINE_STAGE_SORTED,
    ensure_missing_pipeline_entries,
    publication_readiness_for_post,
    sync_pipeline_entry_stage,
)
from app.content.pipeline_logic import (
    READY_DRAFT_STATUS,
    axes_from_label_scores,
    education_eligibility,
    status_from_state,
)
from app.content.pipeline_rewriter import ensure_active_prompt_version, rewrite_entry
from app.content.processor import process_new, process_post
from app.content.state import mark_state
from app.content.search import reindex
from app.content.text_utils import clean_text
from app.content.url_extractor import extract_new, extract_post_links
from app.main import safe_echo
from app.models import (
    ContentItem,
    LinkSnapshot,
    MediaAsset,
    ModelVersion,
    PipelineEntry,
    PostClassification,
    PostProcessed,
    PublicationDraft,
    PublicationTarget,
    PublishedPost,
    Showcase,
    TelegramChat,
    TelegramPost,
)

app = typer.Typer(no_args_is_help=True)
AXES_MODEL_TYPE = "sklearn_bundle_tfidf_logreg_codex_genre_axes"
BULK_UPSERT_ROWS = 1000
ModelContext = tuple[str, str, Path, Any]
FULL_CYCLE_TERMINAL_SKIP_STATUSES = {
    "published",
    "blocked",
    "ineligible",
    "link_summary_failed",
    "rewrite_failed",
    "needs_review",
    MISSING_MEDIA_STATUS,
    PUBLISH_FAILED_STATUS,
    "publish_failed_media",
    "publish_failed_media_verification",
}


@app.callback()
def main() -> None:
    """Publication pipeline manager commands."""


def resolve_artifact_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.exists():
        return candidate
    rooted = Path.cwd() / candidate
    if rooted.exists():
        return rooted
    return candidate


def latest_candidate_from_filesystem() -> tuple[str, Path] | None:
    root = Path("models") / "candidates"
    if not root.exists():
        return None
    candidates = sorted(
        [path for path in root.iterdir() if path.is_dir() and (path / "tfidf_logreg.joblib").exists()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    latest = candidates[0]
    return latest.name, latest / "tfidf_logreg.joblib"


async def bulk_upsert(
    session,
    table,
    values_batch: list[dict[str, Any]],
    *,
    constraint: str,
    preserve_keys: set[str] | None = None,
) -> None:
    """Upsert in chunks under asyncpg's 32767-parameter limit."""

    preserve = preserve_keys or set()
    for index in range(0, len(values_batch), BULK_UPSERT_ROWS):
        chunk = values_batch[index : index + BULK_UPSERT_ROWS]
        stmt = insert(table).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                constraint=constraint,
                set_={key: stmt.excluded[key] for key in chunk[0] if key not in preserve},
            )
        )


async def selected_model(session, settings) -> tuple[str, str, Path, Any]:
    row = None
    if settings.pipeline_use_latest_candidate_model:
        row = (
            await session.execute(
                select(ModelVersion)
                .where(ModelVersion.model_name == "tfidf_logreg", ModelVersion.model_type == AXES_MODEL_TYPE)
                .order_by(ModelVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        row = (
            await session.execute(
                select(ModelVersion)
                .where(ModelVersion.model_name == "tfidf_logreg", ModelVersion.status == "active")
                .order_by(ModelVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is not None:
        model_path = resolve_artifact_path(row.artifact_path)
        return row.model_name, row.model_version, model_path, load(model_path)
    fs_candidate = latest_candidate_from_filesystem()
    if fs_candidate:
        version, model_path = fs_candidate
        return "tfidf_logreg", version, model_path, load(model_path)
    model_path = resolve_artifact_path(settings.classifier_active_model_path)
    return "tfidf_logreg", "filesystem", model_path, load(model_path)


async def selected_model_context(settings) -> ModelContext:
    factory = session_factory(settings)
    async with factory() as session:
        return await selected_model(session, settings)


def prediction_from_model(model: Any, text: str, item: ContentItem, processed: PostProcessed, snapshot: LinkSnapshot | None) -> dict[str, Any]:
    if isinstance(model, dict) and model.get("kind") == "codex_supervised_genre_axes_bundle_v1":
        return media_only_prediction() if is_media_only_unknown(item, processed, snapshot) else predict_bundle(model, text)
    primary, secondary, confidence, scores = predict(model, text)
    return {
        "genre_primary": primary,
        "genre_secondary": secondary,
        "label_scores": scores,
        "confidence": confidence,
        "axes": axes_from_label_scores(scores),
    }


def batch_predict_bundle(bundle: dict[str, Any], texts: list[str]) -> list[dict[str, Any]]:
    genre_model = bundle["genre_model"]
    predicted = [str(value) for value in genre_model.predict(texts)]
    label_scores: list[dict[str, float]] = [{} for _ in texts]
    secondary: list[list[str]] = [[] for _ in texts]
    if hasattr(genre_model, "predict_proba"):
        labels = [str(label) for label in getattr(genre_model, "classes_", [])]
        probabilities = genre_model.predict_proba(texts)
        for index, proba in enumerate(probabilities):
            scores = {label: float(score) for label, score in zip(labels, proba)}
            label_scores[index].update(scores)
            secondary[index] = [
                label
                for label, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
                if label != predicted[index] and score >= 0.20
            ][:3]
    multilabel = bundle.get("genre_multilabel")
    if multilabel:
        ml_model = multilabel["model"]
        mlb = multilabel["binarizer"]
        ml_proba = ml_model.predict_proba(texts) if hasattr(ml_model, "predict_proba") else ml_model.predict(texts)
        for index, proba in enumerate(ml_proba):
            multilabel_scores = {str(label): float(score) for label, score in zip(mlb.classes_, proba)}
            label_scores[index].update({f"multi:{label}": score for label, score in multilabel_scores.items()})
            ranked = [
                (label, score)
                for label, score in sorted(multilabel_scores.items(), key=lambda item: item[1], reverse=True)
                if label != predicted[index]
            ]
            selected = [label for label, score in ranked if score >= 0.25][:3]
            if not selected and ranked and ranked[0][1] >= 0.15:
                selected = [ranked[0][0]]
            if selected:
                secondary[index] = selected
    axes_by_name = {axis: [int(value) for value in bundle["axis_models"][axis].predict(texts)] for axis in bundle["axis_columns"]}
    return [
        {
            "genre_primary": predicted[index],
            "genre_secondary": secondary[index],
            "label_scores": label_scores[index],
            "axes": {axis: axes_by_name[axis][index] for axis in bundle["axis_columns"]},
        }
        for index in range(len(texts))
    ]


def batch_prediction_from_model(model: Any, payloads: list[tuple[str, ContentItem, PostProcessed, LinkSnapshot | None]]) -> list[dict[str, Any]]:
    if isinstance(model, dict) and model.get("kind") == "codex_supervised_genre_axes_bundle_v1":
        predictions: list[dict[str, Any] | None] = [None] * len(payloads)
        non_media_indexes: list[int] = []
        non_media_texts: list[str] = []
        for index, (text, item, processed, snapshot) in enumerate(payloads):
            if is_media_only_unknown(item, processed, snapshot):
                predictions[index] = media_only_prediction()
            else:
                non_media_indexes.append(index)
                non_media_texts.append(text)
        if non_media_texts:
            for index, prediction in zip(non_media_indexes, batch_predict_bundle(model, non_media_texts)):
                predictions[index] = prediction
        return [prediction or media_only_prediction() for prediction in predictions]

    texts = [payload[0] for payload in payloads]
    primary = [str(value) for value in model.predict(texts)]
    labels = [str(label) for label in getattr(model, "classes_", [])]
    predictions: list[dict[str, Any]] = []
    probabilities = model.predict_proba(texts) if hasattr(model, "predict_proba") else None
    for index, label in enumerate(primary):
        scores: dict[str, float] = {}
        confidence = 0.0
        secondary: list[str] = []
        if probabilities is not None:
            scores = {class_label: float(score) for class_label, score in zip(labels, probabilities[index])}
            confidence = max(scores.values()) if scores else 0.0
            secondary = [
                class_label
                for class_label, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
                if class_label != label and score >= 0.20
            ][:3]
        predictions.append(
            {
                "genre_primary": label,
                "genre_secondary": secondary,
                "label_scores": scores,
                "confidence": confidence,
                "axes": axes_from_label_scores(scores),
            }
        )
    return predictions


def confidence_from_prediction(prediction: dict[str, Any]) -> float:
    if prediction.get("confidence") is not None:
        return float(prediction["confidence"])
    primary = prediction.get("genre_primary")
    scores = prediction.get("label_scores") or {}
    if primary in scores:
        return float(scores[primary])
    numeric_scores = [float(value) for key, value in scores.items() if not str(key).startswith("multi:") and isinstance(value, int | float)]
    return max(numeric_scores) if numeric_scores else 0.0


async def classify_content_items(
    limit: int,
    *,
    refresh: bool = False,
    after_id: int = 0,
    model_context: ModelContext | None = None,
) -> tuple[int, int | None, int | None]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    min_item_id: int | None = None
    max_item_id: int | None = None
    async with factory() as session:
        model_name, model_version, _model_path, model = model_context or await selected_model(session, settings)
        stmt = (
            select(ContentItem, PostProcessed, LinkSnapshot)
            .join(TelegramPost, TelegramPost.id == ContentItem.source_post_id)
            .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
            .join(PostProcessed, PostProcessed.post_id == ContentItem.source_post_id)
            .outerjoin(LinkSnapshot, LinkSnapshot.id == ContentItem.primary_snapshot_id)
            .outerjoin(
                PostClassification,
                (PostClassification.post_id == ContentItem.source_post_id)
                & (PostClassification.classifier_name == model_name)
                & (PostClassification.classifier_version == model_version),
            )
            .where(TelegramChat.folder_name == settings.folder_name, ContentItem.id > after_id)
            .order_by(ContentItem.id)
            .limit(limit)
        )
        if not refresh:
            stmt = stmt.where(PostClassification.id.is_(None))
        rows = list((await session.execute(stmt)).all())
        table = PostClassification.__table__
        payloads: list[tuple[str, ContentItem, PostProcessed, LinkSnapshot | None]] = []
        for item, processed, snapshot in rows:
            min_item_id = min(min_item_id or int(item.id), int(item.id))
            max_item_id = max(max_item_id or 0, int(item.id))
            text = build_classification_text(
                processed.clean_text,
                snapshot.title if snapshot else item.title,
                snapshot.description if snapshot else None,
                item.translated_summary or item.source_summary,
                processed.domains,
                {
                    "has_github": processed.has_github,
                    "has_arxiv": processed.has_arxiv,
                    "has_code": processed.has_code,
                    "has_media": processed.has_media,
                    "is_empty": int(processed.word_count or 0) == 0,
                    "word_count": processed.word_count,
                    "url_count": processed.url_count,
                },
            )
            payloads.append((text, item, processed, snapshot))
        predictions = batch_prediction_from_model(model, payloads)
        values_batch: list[dict[str, Any]] = []
        for (text, item, processed, snapshot), prediction in zip(payloads, predictions):
            axes = {key: int(value) for key, value in (prediction.get("axes") or {}).items()}
            scores = dict(prediction.get("label_scores") or {})
            scores["axes"] = axes
            confidence = confidence_from_prediction(prediction)
            needs_review = confidence < settings.classifier_min_confidence
            values = {
                "post_id": item.source_post_id,
                "content_item_id": item.id,
                "classifier_name": model_name,
                "classifier_version": model_version,
                "label_primary": prediction["genre_primary"],
                "label_secondary": list(prediction.get("genre_secondary") or []),
                "label_scores": scores,
                "confidence": confidence,
                "explanation": "Local TF-IDF Codex-supervised genre-axis bundle over post, link metadata, summary, domains and flags.",
                "needs_review": needs_review,
                "created_at": datetime.now(timezone.utc),
            }
            values_batch.append(values)
        if values_batch:
            await bulk_upsert(
                session,
                table,
                values_batch,
                constraint="uq_post_classifications_model",
                preserve_keys={"created_at"},
            )
            count = len(values_batch)
        await session.commit()
    return count, min_item_id, max_item_id


async def classify_post(post_id: int, *, model_context: ModelContext | None = None) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        model_name, model_version, _model_path, model = model_context or await selected_model(session, settings)
        row = (
            await session.execute(
                select(ContentItem, PostProcessed, LinkSnapshot)
                .join(PostProcessed, PostProcessed.post_id == ContentItem.source_post_id)
                .outerjoin(LinkSnapshot, LinkSnapshot.id == ContentItem.primary_snapshot_id)
                .where(ContentItem.source_post_id == post_id)
                .limit(1)
            )
        ).first()
        if not row:
            return 0
        item, processed, snapshot = row
        text = build_classification_text(
            processed.clean_text,
            snapshot.title if snapshot else item.title,
            snapshot.description if snapshot else None,
            item.translated_summary or item.source_summary,
            processed.domains,
            {
                "has_github": processed.has_github,
                "has_arxiv": processed.has_arxiv,
                "has_code": processed.has_code,
                "has_media": processed.has_media,
                "is_empty": int(processed.word_count or 0) == 0,
                "word_count": processed.word_count,
                "url_count": processed.url_count,
            },
        )
        prediction = prediction_from_model(model, text, item, processed, snapshot)
        axes = {key: int(value) for key, value in (prediction.get("axes") or {}).items()}
        scores = dict(prediction.get("label_scores") or {})
        scores["axes"] = axes
        confidence = confidence_from_prediction(prediction)
        values = {
            "post_id": item.source_post_id,
            "content_item_id": item.id,
            "classifier_name": model_name,
            "classifier_version": model_version,
            "label_primary": prediction["genre_primary"],
            "label_secondary": list(prediction.get("genre_secondary") or []),
            "label_scores": scores,
            "confidence": confidence,
            "explanation": "Local TF-IDF Codex-supervised genre-axis bundle over post, link metadata, summary, domains and flags.",
            "needs_review": confidence < settings.classifier_min_confidence,
            "created_at": datetime.now(timezone.utc),
        }
        stmt = insert(PostClassification.__table__).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_post_classifications_model",
                set_={key: stmt.excluded[key] for key in values if key != "created_at"},
            )
        )
        await sync_pipeline_entry_stage(session, post_id)
        await session.commit()
        return 1


async def ai_education_showcase(session, settings) -> Showcase:
    showcase = (
        await session.execute(select(Showcase).where(Showcase.slug == settings.pipeline_showcase_slug).limit(1))
    ).scalar_one_or_none()
    if showcase:
        return showcase
    fallback = (await session.execute(select(Showcase).where(Showcase.slug == "ai_education").limit(1))).scalar_one_or_none()
    if fallback:
        return fallback
    raise RuntimeError("AI education showcase is missing.")


async def refresh_pipeline_entries(
    limit: int,
    *,
    after_id: int = 0,
    through_id: int | None = None,
    model_context: ModelContext | None = None,
) -> tuple[int, int | None]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    max_item_id: int | None = None
    async with factory() as session:
        model_name, model_version, _model_path, _model = model_context or await selected_model(session, settings)
        showcase = await ai_education_showcase(session, settings)
        stmt = (
            select(ContentItem, TelegramPost, TelegramChat, PostClassification)
            .join(TelegramPost, TelegramPost.id == ContentItem.source_post_id)
            .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
            .join(
                PostClassification,
                (PostClassification.content_item_id == ContentItem.id)
                & (PostClassification.classifier_name == model_name)
                & (PostClassification.classifier_version == model_version),
            )
            .where(TelegramChat.folder_name == settings.folder_name, ContentItem.id > after_id)
            .order_by(ContentItem.id)
            .limit(limit)
        )
        if through_id is not None:
            stmt = stmt.where(ContentItem.id <= through_id)
        result = await session.execute(stmt)
        rows = list(result.all())
        if not rows:
            return 0, None

        content_ids = [item.id for item, *_ in rows]
        source_post_ids = [item.source_post_id for item, *_ in rows]
        existing_entries = {
            entry.source_post_id: entry
            for entry in (
                await session.execute(select(PipelineEntry).where(PipelineEntry.source_post_id.in_(source_post_ids)))
            ).scalars()
        }
        targets = {
            target.content_item_id: target
            for target in (
                await session.execute(
                    select(PublicationTarget).where(
                        PublicationTarget.content_item_id.in_(content_ids),
                        PublicationTarget.showcase_id == showcase.id,
                    )
                )
            ).scalars()
        }
        drafts_by_post: dict[int, PublicationDraft] = {}
        draft_result = await session.execute(
            select(PublicationDraft).where(PublicationDraft.source_post_id.in_(source_post_ids)).order_by(PublicationDraft.id.desc())
        )
        for draft in draft_result.scalars():
            drafts_by_post.setdefault(draft.source_post_id, draft)
        published_by_draft: dict[int, PublishedPost] = {}
        draft_ids = [draft.id for draft in drafts_by_post.values()]
        if draft_ids:
            published_result = await session.execute(select(PublishedPost).where(PublishedPost.draft_id.in_(draft_ids)))
            for published in published_result.scalars():
                published_by_draft[published.draft_id] = published

        target_table = PublicationTarget.__table__
        entry_table = PipelineEntry.__table__
        target_values_batch: list[dict[str, Any]] = []
        entry_values_batch: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for item, _post, _chat, classification in rows:
            max_item_id = max(max_item_id or 0, int(item.id))
            axes = axes_from_label_scores(classification.label_scores)
            eligibility = education_eligibility(
                classification.label_primary,
                list(classification.label_secondary or []),
                axes,
                needs_review=bool(classification.needs_review),
            )
            if eligibility.is_eligible and item.id not in targets:
                target_values = {
                    "content_item_id": item.id,
                    "showcase_id": showcase.id,
                    "route_reason": "pipeline education_guide difficulty>=2 promo=0 event=0",
                    "route_score": float(classification.confidence or 0),
                    "status": "pending",
                    "created_at": now,
                    "updated_at": now,
                }
                target_values_batch.append(target_values)
            existing = existing_entries.get(item.source_post_id)
            allowed = bool(existing.publication_allowed) if existing else True
            draft = drafts_by_post.get(item.source_post_id)
            published = published_by_draft.get(draft.id) if draft else None
            status = status_from_state(
                publication_allowed=allowed,
                is_eligible=eligibility.is_eligible,
                draft_status=draft.status if draft else None,
                has_published_post=published is not None,
                has_error=bool(existing.last_error) if existing else False,
            )
            if (
                existing
                and existing.status in {LINK_SUMMARY_PENDING_STATUS, LINK_SUMMARY_FAILED_STATUS, "rewrite_running"}
                and not draft
                and published is None
            ):
                status = existing.status
            stage = (
                PIPELINE_STAGE_PUBLISHED
                if published
                else PIPELINE_STAGE_READY
                if draft and draft.status == READY_DRAFT_STATUS
                else PIPELINE_STAGE_REWRITTEN
                if draft
                else PIPELINE_STAGE_ENRICHED
                if eligibility.is_eligible
                else PIPELINE_STAGE_SORTED
            )
            values = {
                "source_post_id": item.source_post_id,
                "content_item_id": item.id,
                "classification_id": classification.id,
                "classification_model_version": model_version,
                "genre_primary": classification.label_primary,
                "genre_secondary": list(classification.label_secondary or []),
                "confidence": float(classification.confidence or 0),
                "difficulty_score": axes["difficulty_score"],
                "promo_score": axes["promo_score"],
                "opinion_score": axes["opinion_score"],
                "event_score": axes["event_score"],
                "is_eligible": eligibility.is_eligible,
                "eligibility_reason": eligibility.reason,
                "publication_allowed": True,
                "blocked_reason": None,
                "scheduled_publish_at": now + timedelta(hours=settings.pipeline_default_publish_delay_hours) if eligibility.is_eligible else None,
                "latest_draft_id": draft.id if draft else None,
                "published_post_id": published.id if published else None,
                "stage": stage,
                "status": status,
                "last_error": existing.last_error if existing else None,
                "created_at": now,
                "updated_at": now,
                "last_operation_at": now,
            }
            entry_values_batch.append(values)
            count += 1
        if target_values_batch:
            await bulk_upsert(
                session,
                target_table,
                target_values_batch,
                constraint="uq_publication_targets_item_showcase",
                preserve_keys={"created_at"},
            )
        if entry_values_batch:
            await bulk_upsert(
                session,
                entry_table,
                entry_values_batch,
                constraint="uq_pipeline_entries_source_post",
                preserve_keys={"publication_allowed", "blocked_reason", "scheduled_publish_at", "created_at"},
            )
            for post_id in source_post_ids:
                await sync_pipeline_entry_stage(session, int(post_id))
        await session.commit()
    return count, max_item_id


async def run_local_stages(limit: int) -> dict[str, Any]:
    settings = settings_or_exit()
    results: dict[str, Any] = {}
    results["processed"] = await process_new(limit)
    results["links"] = await extract_new(limit)
    results["enriched"] = await enrich_pending(min(limit, 200))
    results["telegram_media"] = await register_telegram_media(min(limit, 500))
    results["link_images"] = await download_link_images(min(limit, 200))
    results["summaries"] = await summarize_pending(min(limit, 200))
    results["content_items"] = await build_new(limit)
    results["translations"] = await translate_pending(limit)
    classified, _min_id, _max_id = await classify_content_items(limit)
    refreshed, _ = await refresh_pipeline_entries(limit)
    results["classifications"] = classified
    results["pipeline_entries"] = refreshed
    results["search_documents"] = await reindex(limit)
    return results


async def run_post_processing_cycle(post_id: int, *, model_context: ModelContext | None = None) -> dict[str, int]:
    results = {
        "processed": await process_post(post_id),
        "links": await extract_post_links(post_id),
        "enriched": await enrich_post_links(post_id),
        "telegram_media": await register_telegram_media_for_post(post_id),
        "link_images": await download_link_images_for_post(post_id),
        "summaries": await summarize_post_links(post_id),
        "content_items": await build_post_material(post_id, refresh=True),
        "classifications": await classify_post(post_id, model_context=model_context),
    }
    settings = settings_or_exit()
    async with session_factory(settings)() as session:
        await sync_pipeline_entry_stage(session, post_id)
        await session.commit()
    return results


async def catch_up_pipeline(limit: int) -> dict[str, int]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        created = await ensure_missing_pipeline_entries(session, folder_name=settings.folder_name, limit=limit)
        await session.commit()
        post_ids = list(
            (
                await session.execute(
                    select(PipelineEntry.source_post_id)
                    .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                    .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
                    .where(
                        TelegramChat.folder_name == settings.folder_name,
                        PipelineEntry.stage == PIPELINE_STAGE_RECEIVED,
                    )
                    .order_by(PipelineEntry.last_operation_at.desc(), PipelineEntry.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )
    model_context = await selected_model_context(settings)
    totals = {
        "received_entries": int(created),
        "posts": 0,
        "processed": 0,
        "links": 0,
        "enriched": 0,
        "telegram_media": 0,
        "link_images": 0,
        "summaries": 0,
        "content_items": 0,
        "classifications": 0,
    }
    for post_id in post_ids:
        totals["posts"] += 1
        result = await run_post_processing_cycle(int(post_id), model_context=model_context)
        for key, value in result.items():
            totals[key] += int(value or 0)
    return totals


def full_cycle_report_path(settings, path: str | None = None) -> Path:
    if path:
        return Path(path)
    return Path(settings.reports_dir) / f"full_cycle_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.jsonl"


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


async def select_full_cycle_candidate(session, settings) -> PipelineEntry | None:
    await ensure_missing_pipeline_entries(session, folder_name=settings.folder_name, limit=250)
    await session.commit()
    direct_image_exists = (
        select(MediaAsset.id)
        .where(
            MediaAsset.source_post_id == PipelineEntry.source_post_id,
            MediaAsset.download_status == "done",
            MediaAsset.mime_type.ilike("image/%"),
        )
        .exists()
    )
    link_image_exists = (
        select(MediaAsset.id)
        .join(LinkSnapshot, LinkSnapshot.image_asset_id == MediaAsset.id)
        .join(PostLink, PostLink.id == LinkSnapshot.link_id)
        .where(
            PostLink.post_id == PipelineEntry.source_post_id,
            MediaAsset.download_status == "done",
            MediaAsset.mime_type.ilike("image/%"),
        )
        .exists()
    )
    image_exists = or_(direct_image_exists, link_image_exists)
    return (
        await session.execute(
            select(PipelineEntry)
            .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
            .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
            .outerjoin(PublishedPost, PublishedPost.id == PipelineEntry.published_post_id)
            .where(
                TelegramChat.folder_name == settings.folder_name,
                TelegramPost.is_deleted.is_(False),
                PipelineEntry.publication_allowed.is_(True),
                PipelineEntry.stage != PIPELINE_STAGE_PUBLISHED,
                PipelineEntry.published_post_id.is_(None),
                PipelineEntry.status.notin_(FULL_CYCLE_TERMINAL_SKIP_STATUSES),
                PublishedPost.id.is_(None),
            )
            .order_by(
                sql_case((PipelineEntry.is_eligible.is_(True), 0), (PipelineEntry.classification_id.is_(None), 2), else_=1),
                sql_case((image_exists, 0), else_=1),
                PipelineEntry.last_operation_at.desc(),
                PipelineEntry.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def mark_entry_status(session, entry: PipelineEntry, *, status: str, error: str | None = None) -> None:
    await session.execute(
        update(PipelineEntry)
        .where(PipelineEntry.id == entry.id)
        .values(
            status=status,
            last_error=error,
            updated_at=datetime.now(timezone.utc),
            last_operation_at=datetime.now(timezone.utc),
        )
    )
    await sync_pipeline_entry_stage(session, entry.source_post_id)


async def run_tracked_post_stage(
    settings,
    post_id: int,
    *,
    status_field: str,
    phase: str,
    action,
    mark_done: bool = True,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    before = datetime.now(timezone.utc)
    async with session_factory(settings)() as session:
        await mark_state(session, post_id, **{status_field: "running", "last_error": ""})
        await session.commit()
    try:
        value = await action()
        if mark_done:
            async with session_factory(settings)() as session:
                await mark_state(session, post_id, **{status_field: "done"})
                await sync_pipeline_entry_stage(session, post_id)
                await session.commit()
        return {
            "phase": phase,
            "status": "done",
            "result": value,
            "started_at": started.isoformat(),
            "seconds": round((datetime.now(timezone.utc) - before).total_seconds(), 2),
        }
    except Exception as exc:
        error = str(exc)[:800]
        async with session_factory(settings)() as session:
            await mark_state(session, post_id, **{status_field: "failed", "last_error": error})
            await sync_pipeline_entry_stage(session, post_id)
            await session.commit()
        return {
            "phase": phase,
            "status": "failed",
            "error": error,
            "started_at": started.isoformat(),
            "seconds": round((datetime.now(timezone.utc) - before).total_seconds(), 2),
        }


async def pipeline_entry_context(session, entry_id: int):
    return (
        await session.execute(
            select(PipelineEntry, ContentItem, PublicationDraft, PublicationTarget, Showcase)
            .outerjoin(ContentItem, ContentItem.id == PipelineEntry.content_item_id)
            .outerjoin(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
            .outerjoin(PublicationTarget, PublicationTarget.id == PublicationDraft.publication_target_id)
            .outerjoin(Showcase, Showcase.id == PublicationTarget.showcase_id)
            .where(PipelineEntry.id == entry_id)
            .limit(1)
        )
    ).first()


async def ensure_full_cycle_rewrite(settings, entry_id: int) -> dict[str, Any]:
    async with session_factory(settings)() as session:
        prompt_version = await ensure_active_prompt_version(session)
        row = await pipeline_entry_context(session, entry_id)
        if not row:
            return {"rewritten": 0, "error": "entry_not_found"}
        entry, _item, draft, _target, _showcase = row
        if draft and draft.status == READY_DRAFT_STATUS:
            await mark_state(session, entry.source_post_id, rewrite_status=READY_DRAFT_STATUS)
            await session.commit()
            return {"rewritten": 0, "skipped": "draft_already_ready", "draft_id": draft.id}
        ok = await rewrite_entry(
            session,
            entry,
            prompt_version=prompt_version,
            refresh=bool(draft),
            allow_pending_link_summaries=False,
        )
        await session.commit()
        return {"rewritten": 1 if ok else 0}


async def publish_full_cycle_entry(settings, entry_id: int) -> dict[str, Any]:
    async with session_factory(settings)() as session:
        row = await pipeline_entry_context(session, entry_id)
        if not row:
            return {"attempted": 0, "published": 0, "failed": 1, "status": "entry_not_found"}
        entry, _item, draft, _target, showcase = row
        if not draft or not showcase:
            await mark_entry_status(session, entry, status="rewrite_failed", error="publish_context_missing")
            await session.commit()
            return {"attempted": 0, "published": 0, "failed": 1, "status": "publish_context_missing"}
        if draft.status != READY_DRAFT_STATUS or draft.validation_errors:
            await mark_state(session, entry.source_post_id, publication_status="pending")
            await session.commit()
            return {
                "attempted": 0,
                "published": 0,
                "failed": 1,
                "status": "draft_not_ready",
                "draft_status": draft.status,
                "validation_errors": list(draft.validation_errors or []),
            }
        result = await publish_ready_entry(session, settings, entry, draft, showcase)
        await session.commit()
        return result


async def run_full_cycle_once(*, model_context: ModelContext | None = None) -> dict[str, Any]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        candidate = await select_full_cycle_candidate(session, settings)
        if not candidate:
            return {"processed": 0, "status": "no_candidate"}
        entry_id = int(candidate.id)
        post_id = int(candidate.source_post_id)
    lock = await try_acquire_pipeline_work_lock(settings, owner="full_cycle", entry_id=entry_id)
    if lock is None:
        return {"processed": 0, "status": "busy", "entry_id": entry_id, "post_id": post_id}
    events: list[dict[str, Any]] = []
    try:
        model_context = model_context or await selected_model_context(settings)
        for status_field, phase, action in [
            ("processing_status", "process_post", lambda: process_post(post_id)),
            ("link_status", "extract_links", lambda: extract_post_links(post_id)),
            ("enrichment_status", "enrich_links", lambda: enrich_post_links(post_id)),
            ("enrichment_status", "register_telegram_media", lambda: register_telegram_media_for_post(post_id)),
            ("enrichment_status", "download_link_images", lambda: download_link_images_for_post(post_id)),
            ("summary_status", "summarize_links", lambda: summarize_post_links(post_id)),
            ("material_status", "build_material", lambda: build_post_material(post_id, refresh=True)),
            ("classification_status", "classify_post", lambda: classify_post(post_id, model_context=model_context)),
        ]:
            event = await run_tracked_post_stage(settings, post_id, status_field=status_field, phase=phase, action=action)
            events.append(event)
            if event["status"] == "failed":
                return {"processed": 1, "entry_id": entry_id, "post_id": post_id, "status": event["phase"] + "_failed", "events": events}

        async with factory() as session:
            entry = await sync_pipeline_entry_stage(session, post_id)
            row = await pipeline_entry_context(session, entry_id)
            _entry, item, draft, _target, _showcase = row if row else (entry, None, None, None, None)
            if not entry:
                return {"processed": 1, "entry_id": entry_id, "post_id": post_id, "status": "entry_missing", "events": events}
            if not entry.is_eligible:
                await session.commit()
                return {"processed": 1, "entry_id": entry.id, "post_id": post_id, "status": "ineligible", "events": events}
            readiness = await publication_readiness_for_post(
                session,
                post_id,
                item=item,
                draft=draft,
                is_eligible=bool(entry.is_eligible),
                publication_allowed=bool(entry.publication_allowed),
            )
            selected_media = await select_publish_media(session, post_id, draft)
            if not selected_media:
                await mark_entry_status(session, entry, status=MISSING_MEDIA_STATUS, error="missing publishable image media")
                await mark_state(session, post_id, publication_status=MISSING_MEDIA_STATUS, last_error="missing publishable image media")
                await session.commit()
                return {
                    "processed": 1,
                    "entry_id": entry.id,
                    "post_id": post_id,
                    "status": MISSING_MEDIA_STATUS,
                    "missing": list(readiness.missing),
                    "events": events,
                }
            await session.commit()

        rewrite_event = await run_tracked_post_stage(
            settings,
            post_id,
            status_field="rewrite_status",
            phase="rewrite",
            action=lambda: ensure_full_cycle_rewrite(settings, entry_id),
        )
        events.append(rewrite_event)
        if rewrite_event["status"] == "failed":
            return {"processed": 1, "entry_id": entry_id, "post_id": post_id, "status": "rewrite_failed", "events": events}
        rewrite_result = rewrite_event.get("result") if isinstance(rewrite_event.get("result"), dict) else {}
        if not int(rewrite_result.get("rewritten") or 0) and rewrite_result.get("skipped") != "draft_already_ready":
            async with factory() as session:
                refreshed = await sync_pipeline_entry_stage(session, post_id)
                await session.commit()
            return {
                "processed": 1,
                "entry_id": entry_id,
                "post_id": post_id,
                "status": refreshed.status if refreshed else "rewrite_blocked",
                "events": events,
            }

        publish_event = await run_tracked_post_stage(
            settings,
            post_id,
            status_field="publication_status",
            phase="publish",
            action=lambda: publish_full_cycle_entry(settings, entry_id),
            mark_done=False,
        )
        events.append(publish_event)
        publish_result = publish_event.get("result") if isinstance(publish_event.get("result"), dict) else {}
        status = publish_result.get("status") or ("published" if publish_result.get("published") else "publish_failed")
        return {
            "processed": 1,
            "entry_id": entry_id,
            "post_id": post_id,
            "status": status,
            "published": int(publish_result.get("published") or 0),
            "message_id": publish_result.get("message_id"),
            "published_url": publish_result.get("published_url"),
            "media_asset_id": publish_result.get("media_asset_id"),
            "events": events,
        }
    finally:
        await lock.release()


async def run_full_cycle(limit: int, *, canary_report: str | None = None, busy_retry_seconds: int = 10) -> dict[str, Any]:
    settings = settings_or_exit()
    report_path = full_cycle_report_path(settings, canary_report)
    model_context = await selected_model_context(settings)
    rows: list[dict[str, Any]] = []
    processed_count = 0
    busy_retries = 0
    while processed_count < limit:
        row = await run_full_cycle_once(model_context=model_context)
        if row.get("processed"):
            processed_count += int(row.get("processed") or 0)
            busy_retries = 0
        elif row.get("status") == "busy":
            busy_retries += 1
        row["index"] = processed_count if row.get("processed") else processed_count + 1
        row["written_at"] = datetime.now(timezone.utc).isoformat()
        append_jsonl(report_path, row)
        rows.append(row)
        safe_echo(
            " ".join(
                [
                    f"full_cycle={processed_count}/{limit}",
                    f"entry_id={row.get('entry_id')}",
                    f"post_id={row.get('post_id')}",
                    f"status={row.get('status')}",
                    f"published={row.get('published', 0)}",
                ]
            )
        )
        sys.stdout.flush()
        if row.get("status") == "no_candidate":
            break
        if row.get("status") == "busy":
            if busy_retries >= 12:
                break
            await asyncio.sleep(busy_retry_seconds)
    return {
        "processed": sum(int(row.get("processed") or 0) for row in rows),
        "published": sum(int(row.get("published") or 0) for row in rows),
        "failed": sum(1 for row in rows if row.get("processed") and not row.get("published")),
        "busy": sum(1 for row in rows if row.get("status") == "busy"),
        "report": str(report_path),
    }


async def run_all_classify_and_refresh(limit: int, *, refresh_classification: bool = False) -> dict[str, int]:
    settings = settings_or_exit()
    model_context = await selected_model_context(settings)
    classified_total = 0
    refreshed_total = 0
    after_id = 0
    while True:
        classified, _min_id, max_id = await classify_content_items(
            limit,
            refresh=refresh_classification,
            after_id=after_id,
            model_context=model_context,
        )
        classified_total += classified
        if max_id is None:
            break
        after_id = max_id
    after_id = 0
    while True:
        refreshed, max_id = await refresh_pipeline_entries(limit, after_id=after_id, model_context=model_context)
        refreshed_total += refreshed
        if max_id is None:
            break
        after_id = max_id
    return {"classified": classified_total, "pipeline_entries": refreshed_total}


async def run_all_refresh_entries_only(limit: int) -> dict[str, int]:
    settings = settings_or_exit()
    model_context = await selected_model_context(settings)
    refreshed_total = 0
    after_id = 0
    while True:
        refreshed, max_id = await refresh_pipeline_entries(limit, after_id=after_id, model_context=model_context)
        refreshed_total += refreshed
        if max_id is None:
            break
        after_id = max_id
    return {"pipeline_entries": refreshed_total}


async def refresh_pipeline_entries_range(
    limit: int,
    *,
    start_after_id: int,
    through_id: int,
    model_context: ModelContext,
) -> int:
    refreshed_total = 0
    after_id = start_after_id
    while True:
        refreshed, max_id = await refresh_pipeline_entries(
            limit,
            after_id=after_id,
            through_id=through_id,
            model_context=model_context,
        )
        refreshed_total += refreshed
        if max_id is None or max_id >= through_id:
            break
        after_id = max_id
    return refreshed_total


async def run_interleaved_classify_and_refresh(limit: int) -> dict[str, int]:
    settings = settings_or_exit()
    model_context = await selected_model_context(settings)
    classified_total = 0
    refreshed_total = 0
    after_id = 0
    while True:
        classified, min_id, max_id = await classify_content_items(limit, after_id=after_id, model_context=model_context)
        if max_id is None:
            break
        after_id = max_id
        classified_total += classified
        refreshed = (
            await refresh_pipeline_entries_range(
                limit,
                start_after_id=(min_id or max_id) - 1,
                through_id=max_id,
                model_context=model_context,
            )
            if min_id is not None
            else 0
        )
        refreshed_total += refreshed
        safe_echo(
            " ".join(
                [
                    f"chunk_min_id={min_id}",
                    f"max_id={after_id}",
                    f"classified={classified}",
                    f"refreshed={refreshed}",
                    f"total_classified={classified_total}",
                    f"total_pipeline_entries={refreshed_total}",
                ]
            )
        )
        sys.stdout.flush()
    return {"classified": classified_total, "pipeline_entries": refreshed_total}


@app.command("classify-new")
def classify_new_command(
    limit: int = limit_option(),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh existing classifications for the selected model version."),
) -> None:
    """Classify MAX content items with the local genre-axis model."""

    count, _min_id, _max_id = run_async(classify_content_items(limit, refresh=refresh))
    safe_echo(f"classified={count}")


@app.command("refresh-entries")
def refresh_entries_command(
    limit: int = limit_option(),
    all_rows: bool = typer.Option(False, "--all"),
    skip_classify: bool = typer.Option(False, "--skip-classify", help="Only build entries for already-classified content items."),
    interleaved: bool = typer.Option(False, "--interleaved", help="Classify and refresh entries chunk by chunk."),
) -> None:
    """Build or refresh publication pipeline entries from local classifications."""

    if all_rows:
        if skip_classify:
            result = run_async(run_all_refresh_entries_only(limit))
        elif interleaved:
            result = run_async(run_interleaved_classify_and_refresh(limit))
        else:
            result = run_async(run_all_classify_and_refresh(limit))
        safe_echo(f"classified={result.get('classified', 0)} pipeline_entries={result['pipeline_entries']}")
        return
    count, _ = run_async(refresh_pipeline_entries(limit))
    safe_echo(f"pipeline_entries={count}")


@app.command("backfill")
def backfill_command(
    limit: int = typer.Option(1000, "--limit", min=1),
    all_rows: bool = typer.Option(False, "--all", help="After local stages, classify and refresh all existing content items."),
    refresh_classification: bool = typer.Option(False, "--refresh-classification"),
) -> None:
    """Run local pipeline stages and build publication pipeline entries without YandexGPT."""

    results = run_async(run_local_stages(limit))
    if all_rows:
        results.update(run_async(run_all_classify_and_refresh(limit, refresh_classification=refresh_classification)))
    safe_echo(" ".join(f"{key}={clean_text(str(value))}" for key, value in results.items()))


@app.command("catch-up")
def catch_up_command(limit: int = typer.Option(1000, "--limit", min=1)) -> None:
    """Create received entries for raw Telegram posts and move fresh backlog through local stages."""

    results = run_async(catch_up_pipeline(limit))
    safe_echo(" ".join(f"{key}={clean_text(str(value))}" for key, value in results.items()))


@app.command("full-cycle")
def full_cycle_command(
    limit: int = typer.Option(30, "--limit", min=1),
    canary_report: str | None = typer.Option(None, "--canary-report"),
) -> None:
    """Run selected MAX posts one by one through enrichment, rewrite, publish, and media verification."""

    safe_echo(run_async(run_full_cycle(limit, canary_report=canary_report)))


@app.command("full-cycle-loop")
def full_cycle_loop_command(
    interval_seconds: int = typer.Option(30, "--interval-seconds", min=5),
    max_cycles: int | None = typer.Option(None, "--max-cycles", min=1),
) -> None:
    """Continuously run one full-cycle candidate per interval."""

    settings = settings_or_exit()
    loop_report = str(Path(settings.reports_dir) / "full_cycle_loop.jsonl")
    cycle = 0
    while True:
        cycle += 1
        result = run_async(run_full_cycle(1, canary_report=loop_report))
        safe_echo(f"ts={datetime.now(timezone.utc).isoformat()} cycle={cycle} result={result}")
        sys.stdout.flush()
        if max_cycles is not None and cycle >= max_cycles:
            break
        import time

        time.sleep(interval_seconds)


@app.command("stats")
def stats_command() -> None:
    """Print aggregate pipeline entry stats."""

    async def _stats() -> dict[str, Any]:
        settings = settings_or_exit()
        factory = session_factory(settings)
        async with factory() as session:
            total = (await session.execute(select(func.count()).select_from(PipelineEntry))).scalar_one()
            rows = await session.execute(select(PipelineEntry.status, func.count()).group_by(PipelineEntry.status))
            return {"total": int(total or 0), "statuses": {status: int(count) for status, count in rows.all()}}

    safe_echo(run_async(_stats()))


if __name__ == "__main__":
    app()
