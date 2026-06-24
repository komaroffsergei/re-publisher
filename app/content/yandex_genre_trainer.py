from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.content.classifier import build_classification_text
from app.content.common import run_async, session_factory, settings_or_exit
from app.content.corpus_builder import DEFAULT_PACKAGE, read_initial_rows, stable_split
from app.main import safe_echo
from app.models import ContentItem, LinkSnapshot, ModelVersion, PostProcessed, YandexGenreClassification

app = typer.Typer(no_args_is_help=True)
MODEL_NAME = "tfidf_logreg"
FALLBACK_GENRE = "media_only_unknown"
LEGACY_LABEL_MAP = {
    "news_digest": "news_announcement",
    "technical_research": "technical_research",
    "tool_product": "tool_product",
    "education_guide": "education_guide",
    "opinion_commentary": "opinion_commentary",
    "business_market": "business_market",
    "promo_career_event": "promo_ad",
    "community_chat": "community_chat",
    "humor_meme": "humor_meme",
}


@app.callback()
def main() -> None:
    """Retrain the existing TF-IDF classifier line with saved YandexGPT labels."""


def map_legacy_label(label: str | None, text: str | None = None) -> str | None:
    mapped = LEGACY_LABEL_MAP.get(str(label or ""))
    if label != "promo_career_event":
        return mapped
    lower = (text or "").lower()
    if any(token in lower for token in ["ваканси", "ищем", "job", "career", "hiring"]):
        return "career_job"
    if any(token in lower for token in ["вебинар", "митап", "конференц", "event", "webinar", "запись"]):
        return "event_webinar"
    return mapped


def include_training_result(
    result: YandexGenreClassification,
    *,
    min_confidence: float,
    exclude_needs_review: bool,
    exclude_media_only: bool,
) -> bool:
    if exclude_needs_review and result.needs_review:
        return False
    if exclude_media_only and result.genre_primary == FALLBACK_GENRE:
        return False
    if result.genre_confidence is not None and float(result.genre_confidence) < min_confidence:
        return False
    return True


def training_text(item: ContentItem, processed: PostProcessed, snapshot: LinkSnapshot | None) -> str:
    return build_classification_text(
        processed.clean_text,
        snapshot.title if snapshot else item.title,
        snapshot.description if snapshot else None,
        item.translated_summary or item.source_summary,
        processed.domains,
        {"has_github": processed.has_github, "has_arxiv": processed.has_arxiv, "has_code": processed.has_code},
    )


def training_row(result: YandexGenreClassification, item: ContentItem, processed: PostProcessed, snapshot: LinkSnapshot | None) -> dict[str, Any]:
    return {
        "id": item.source_post_id,
        "content_item_id": item.id,
        "label": result.genre_primary,
        "text": training_text(item, processed, snapshot),
        "split": stable_split(item.source_post_id),
        "label_confidence": float(result.genre_confidence or 0.0),
        "genre_secondary": ",".join(result.genre_secondary or []),
        "difficulty_score": result.difficulty_score,
        "promo_score": result.promo_score,
        "opinion_score": result.opinion_score,
        "event_score": result.event_score,
        "needs_review": result.needs_review,
        "run_id": result.run_id,
        "model_name": result.model_name,
        "taxonomy_version": result.taxonomy_version,
        "source": "yandex_run",
    }


def initial_training_rows(package_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_initial_rows(package_dir):
        text = str(row.get("text") or "")
        label = map_legacy_label(str(row.get("label") or ""), text)
        if not label or not text.strip():
            continue
        rows.append(
            {
                "id": row.get("id"),
                "content_item_id": "",
                "label": label,
                "text": text,
                "split": row.get("split") or stable_split(row.get("id") or ""),
                "label_confidence": row.get("label_confidence") or 1.0,
                "genre_secondary": row.get("secondary_labels") or "",
                "difficulty_score": "",
                "promo_score": "",
                "opinion_score": "",
                "event_score": "",
                "needs_review": False,
                "run_id": "initial_trainable_mapped",
                "model_name": "initial_corpus",
                "taxonomy_version": "yandex_axes_v1",
                "source": "initial_corpus_mapped",
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "id",
        "content_item_id",
        "label",
        "text",
        "split",
        "label_confidence",
        "genre_secondary",
        "difficulty_score",
        "promo_score",
        "opinion_score",
        "event_score",
        "needs_review",
        "run_id",
        "model_name",
        "taxonomy_version",
        "source",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def corpus_stats(rows: list[dict[str, Any]], run_id: str, min_confidence: float, yandex_rows: int, initial_rows: int) -> dict[str, Any]:
    labels: dict[str, int] = {}
    splits: dict[str, int] = {"train": 0, "val": 0, "test": 0}
    for row in rows:
        labels[str(row["label"])] = labels.get(str(row["label"]), 0) + 1
        splits[str(row["split"])] = splits.get(str(row["split"]), 0) + 1
    return {
        "run_id": run_id,
        "rows": len(rows),
        "splits": splits,
        "labels": labels,
        "initial_rows": initial_rows,
        "yandex_rows": yandex_rows,
        "min_confidence": min_confidence,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def collect_training_rows(
    run_id: str,
    *,
    min_confidence: float,
    exclude_needs_review: bool,
    exclude_media_only: bool,
) -> list[dict[str, Any]]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        result = await session.execute(
            select(YandexGenreClassification, ContentItem, PostProcessed, LinkSnapshot)
            .join(ContentItem, ContentItem.id == YandexGenreClassification.content_item_id)
            .join(PostProcessed, PostProcessed.post_id == ContentItem.source_post_id)
            .outerjoin(LinkSnapshot, LinkSnapshot.id == ContentItem.primary_snapshot_id)
            .where(YandexGenreClassification.run_id == run_id)
            .order_by(YandexGenreClassification.id)
        )
        rows: list[dict[str, Any]] = []
        for classification, item, processed, snapshot in result.all():
            if not include_training_result(
                classification,
                min_confidence=min_confidence,
                exclude_needs_review=exclude_needs_review,
                exclude_media_only=exclude_media_only,
            ):
                continue
            rows.append(training_row(classification, item, processed, snapshot))
        return rows


def write_training_corpus(target: Path, rows: list[dict[str, Any]], run_id: str, min_confidence: float, yandex_rows: int, initial_rows: int) -> None:
    write_csv(target / "full.csv", rows)
    write_csv(target / "train.csv", [row for row in rows if row["split"] == "train"])
    write_csv(target / "val.csv", [row for row in rows if row["split"] == "val"])
    write_csv(target / "test.csv", [row for row in rows if row["split"] == "test"])
    with (target / "full.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    stats = corpus_stats(rows, run_id, min_confidence, yandex_rows, initial_rows)
    (target / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


async def build_corpus(
    run_id: str,
    min_confidence: float,
    exclude_needs_review: bool,
    exclude_media_only: bool,
    package_dir: Path,
) -> Path:
    settings = settings_or_exit()
    target = Path(settings.artifacts_dir) / "yandex_genre_training" / run_id
    initial_rows = initial_training_rows(package_dir)
    yandex_rows = await collect_training_rows(
        run_id,
        min_confidence=min_confidence,
        exclude_needs_review=exclude_needs_review,
        exclude_media_only=exclude_media_only,
    )
    rows_by_id = {str(row["id"]): row for row in initial_rows}
    for row in yandex_rows:
        rows_by_id[str(row["id"])] = row
    rows = list(rows_by_id.values())
    write_training_corpus(target, rows, run_id, min_confidence, len(yandex_rows), len(initial_rows))
    return target


def train_pipeline(corpus_dir: Path, model_dir: Path) -> tuple[Path, dict[str, Any]]:
    import pandas as pd
    from joblib import dump
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline

    train_path = corpus_dir / "train.csv"
    data = pd.read_csv(train_path).dropna(subset=["text", "label"])
    if len(data) < 2:
        raise ValueError("At least two training rows are required.")
    if data["label"].nunique() < 2:
        raise ValueError("At least two labels are required.")
    model_dir.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(
        [
            ("tfidf", TfidfVectorizer(lowercase=True, ngram_range=(1, 2), min_df=2, max_df=0.95, max_features=100_000, sublinear_tf=True)),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
        ]
    )
    pipeline.fit(data["text"].astype(str), data["label"].astype(str))
    artifact_path = model_dir / "tfidf_logreg.joblib"
    dump(pipeline, artifact_path)
    metadata = {
        "train_rows": int(len(data)),
        "labels": sorted(str(label) for label in data["label"].unique()),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (model_dir / "training_config.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact_path, metadata


def model_version_values(model_version: str, artifact_path: Path, run_id: str, stats: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_name": MODEL_NAME,
        "model_version": model_version,
        "model_type": "sklearn_pipeline_tfidf_logreg_yandex_augmented",
        "artifact_path": str(artifact_path),
        "label_schema_version": "yandex_axes_v1",
        "train_corpus_hash": run_id,
        "train_size": stats.get("splits", {}).get("train"),
        "val_size": stats.get("splits", {}).get("val"),
        "test_size": stats.get("splits", {}).get("test"),
        "metrics": {"training": metadata, "corpus": stats},
        "confusion_matrix": {},
        "status": "candidate",
        "created_at": datetime.now(timezone.utc),
    }


async def train(run_id: str, min_confidence: float, exclude_needs_review: bool, exclude_media_only: bool, package_dir: Path) -> str:
    settings = settings_or_exit()
    corpus_dir = await build_corpus(run_id, min_confidence, exclude_needs_review, exclude_media_only, package_dir)
    stats = json.loads((corpus_dir / "stats.json").read_text(encoding="utf-8"))
    model_version = f"{run_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    model_dir = Path("models") / "candidates" / model_version
    artifact_path, metadata = train_pipeline(corpus_dir, model_dir)
    factory = session_factory(settings)
    async with factory() as session:
        table = ModelVersion.__table__
        values = model_version_values(model_version, artifact_path, run_id, stats, metadata)
        stmt = insert(table).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_model_versions_name_version",
                set_={key: stmt.excluded[key] for key in values if key != "created_at"},
            )
        )
        await session.commit()
    return model_version


@app.command("build-corpus")
def build_corpus_command(
    run_id: str = typer.Option(..., "--run-id"),
    min_confidence: float = typer.Option(0.65, "--min-confidence", min=0.0, max=1.0),
    exclude_needs_review: bool = typer.Option(True, "--exclude-needs-review/--include-needs-review"),
    exclude_media_only: bool = typer.Option(True, "--exclude-media-only/--include-media-only"),
    package_dir: Path = typer.Option(DEFAULT_PACKAGE, "--package-dir", file_okay=False),
) -> None:
    """Export a training corpus from the initial corpus plus a saved YandexGPT genre run."""

    target = run_async(build_corpus(run_id, min_confidence, exclude_needs_review, exclude_media_only, package_dir))
    safe_echo(f"corpus_dir={target}")


@app.command("train")
def train_command(
    run_id: str = typer.Option(..., "--run-id"),
    min_confidence: float = typer.Option(0.65, "--min-confidence", min=0.0, max=1.0),
    exclude_needs_review: bool = typer.Option(True, "--exclude-needs-review/--include-needs-review"),
    exclude_media_only: bool = typer.Option(True, "--exclude-media-only/--include-media-only"),
    package_dir: Path = typer.Option(DEFAULT_PACKAGE, "--package-dir", file_okay=False),
) -> None:
    """Retrain and register a tfidf_logreg candidate from initial corpus plus a saved YandexGPT genre run."""

    model_version = run_async(train(run_id, min_confidence, exclude_needs_review, exclude_media_only, package_dir))
    safe_echo(f"candidate_model_version={model_version}")


if __name__ == "__main__":
    app()
