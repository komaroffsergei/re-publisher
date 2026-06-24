from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.state import mark_state
from app.content.text_utils import clean_text
from app.main import safe_echo
from app.models import ContentItem, LabelingQueue, LinkSnapshot, ModelVersion, PostClassification, PostLabel, PostProcessed

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Classifier commands."""


def build_classification_text(
    clean_post_text: str | None,
    link_title: str | None = None,
    link_description: str | None = None,
    link_summary_ru: str | None = None,
    domains: list[str] | None = None,
    flags: dict[str, Any] | None = None,
) -> str:
    flags = flags or {}
    return "\n\n".join(
        [
            f"POST:\n{clean_text(clean_post_text)}",
            f"LINK_TITLE:\n{clean_text(link_title)}",
            f"LINK_DESCRIPTION:\n{clean_text(link_description)}",
            f"LINK_SUMMARY_RU:\n{clean_text(link_summary_ru)}",
            f"DOMAINS:\n{' '.join(domains or [])}",
            "FLAGS:\n"
            + " ".join(
                [
                    f"has_github={bool(flags.get('has_github'))}",
                    f"has_arxiv={bool(flags.get('has_arxiv'))}",
                    f"has_code={bool(flags.get('has_code'))}",
                    f"has_media={bool(flags.get('has_media'))}",
                    f"is_empty={bool(flags.get('is_empty'))}",
                    f"word_count={int(flags.get('word_count') or 0)}",
                    f"url_count={int(flags.get('url_count') or 0)}",
                ]
            ),
        ]
    ).strip()


async def active_model_info(session, settings) -> tuple[str, str, Path]:
    result = await session.execute(
        select(ModelVersion).where(ModelVersion.model_name == "tfidf_logreg", ModelVersion.status == "active").order_by(ModelVersion.id.desc()).limit(1)
    )
    row = result.scalar_one_or_none()
    if row:
        return row.model_name, row.model_version, Path(row.artifact_path)
    return "tfidf_logreg", "filesystem", Path(settings.classifier_active_model_path)


def load_model(path: Path):
    from joblib import load

    if not path.exists():
        raise FileNotFoundError(f"Classifier artifact not found: {path}")
    return load(path)


def predict(model, text: str) -> tuple[str, list[str], float, dict[str, float]]:
    labels = list(getattr(model, "classes_", []))
    predicted = str(model.predict([text])[0])
    scores: dict[str, float] = {}
    confidence = 0.0
    secondary: list[str] = []
    if hasattr(model, "predict_proba"):
        proba = model.predict_proba([text])[0]
        scores = {str(label): float(score) for label, score in zip(labels, proba)}
        confidence = float(max(proba))
        secondary = [label for label, score in sorted(scores.items(), key=lambda item: item[1], reverse=True) if label != predicted and score >= 0.20][:3]
    return predicted, secondary, confidence, scores


async def ensure_labeling_queue(session, post_id: int, label: str, confidence: float, reason: str) -> None:
    existing = await session.execute(
        select(LabelingQueue.id).where(LabelingQueue.post_id == post_id, LabelingQueue.status == "pending").limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return
    await session.execute(
        insert(LabelingQueue.__table__).values(
            post_id=post_id,
            reason=reason,
            suggested_label=label,
            suggested_confidence=confidence,
            status="pending",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )


async def classify_new(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        model_name, model_version, model_path = await active_model_info(session, settings)
        model = load_model(model_path)
        result = await session.execute(
            select(ContentItem, PostProcessed, LinkSnapshot)
            .join(PostProcessed, PostProcessed.post_id == ContentItem.source_post_id)
            .outerjoin(LinkSnapshot, LinkSnapshot.id == ContentItem.primary_snapshot_id)
            .outerjoin(
                PostClassification,
                (PostClassification.post_id == ContentItem.source_post_id)
                & (PostClassification.classifier_name == model_name)
                & (PostClassification.classifier_version == model_version),
            )
            .where(PostClassification.id.is_(None))
            .order_by(ContentItem.id)
            .limit(limit)
        )
        rows = list(result.all())
        table = PostClassification.__table__
        for item, processed, snapshot in rows:
            text = build_classification_text(
                processed.clean_text,
                snapshot.title if snapshot else item.title,
                snapshot.description if snapshot else None,
                item.translated_summary or item.source_summary,
                processed.domains,
                {"has_github": processed.has_github, "has_arxiv": processed.has_arxiv, "has_code": processed.has_code},
            )
            label, secondary, confidence, scores = predict(model, text)
            needs_review = confidence < settings.classifier_min_confidence
            values = {
                "post_id": item.source_post_id,
                "content_item_id": item.id,
                "classifier_name": model_name,
                "classifier_version": model_version,
                "label_primary": label,
                "label_secondary": secondary,
                "label_scores": scores,
                "confidence": confidence,
                "explanation": "TF-IDF LogisticRegression over post, link metadata, summary, domains and flags.",
                "needs_review": needs_review,
                "created_at": datetime.now(timezone.utc),
            }
            stmt = insert(table).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_post_classifications_model",
                    set_={key: stmt.excluded[key] for key in values if key not in {"created_at"}},
                )
            )
            if needs_review:
                await ensure_labeling_queue(session, item.source_post_id, label, confidence, "classifier_low_confidence")
            elif confidence >= settings.classifier_high_confidence:
                await session.execute(
                    insert(PostLabel.__table__).values(
                        post_id=item.source_post_id,
                        label=label,
                        label_set_version=model_version,
                        confidence=confidence,
                        source="model_high_confidence",
                        status="proposed",
                        created_by="classifier",
                        raw={"scores": scores},
                    )
                )
            await mark_state(session, item.source_post_id, classification_status="review" if needs_review else "done")
            count += 1
        await session.commit()
    return count


@app.command("classify-new")
def classify_new_command(limit: int = limit_option()) -> None:
    """Classify unclassified content items using the active TF-IDF model."""

    count = run_async(classify_new(limit))
    safe_echo(f"classified={count}")


if __name__ == "__main__":
    app()
