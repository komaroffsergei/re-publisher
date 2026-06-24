from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import typer
from sqlalchemy.dialects.postgresql import insert

from app.content.common import run_async, session_factory, settings_or_exit
from app.main import safe_echo
from app.models import ModelVersion

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Model training commands."""


def latest_corpus_dir(artifacts_dir: str) -> Path:
    root = Path(artifacts_dir) / "corpus"
    candidates = sorted([path for path in root.glob("*") if path.is_dir()])
    if not candidates:
        raise FileNotFoundError(f"No corpus directories under {root}")
    return candidates[-1]


async def train_candidate(corpus_dir: Path | None = None) -> str:
    settings = settings_or_exit()
    corpus_dir = corpus_dir or latest_corpus_dir(settings.artifacts_dir)
    import pandas as pd
    from joblib import dump
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline

    train = pd.read_csv(corpus_dir / "train.csv").dropna(subset=["text", "label"])
    model_version = datetime.now(timezone.utc).strftime("candidate_%Y%m%d_%H%M%S")
    model_dir = Path("models") / "candidates" / model_version
    model_dir.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(
        [
            ("tfidf", TfidfVectorizer(lowercase=True, ngram_range=(1, 2), min_df=2, max_df=0.95, max_features=100_000, sublinear_tf=True)),
            ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
        ]
    )
    pipeline.fit(train["text"].astype(str), train["label"].astype(str))
    artifact_path = model_dir / "tfidf_logreg.joblib"
    dump(pipeline, artifact_path)
    config = {
        "corpus_dir": str(corpus_dir),
        "train_rows": int(len(train)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (model_dir / "training_config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    schema = Path("config/label_schema.yaml")
    if schema.exists():
        (model_dir / "label_schema.yaml").write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")

    factory = session_factory(settings)
    stats = json.loads((corpus_dir / "corpus_stats.json").read_text(encoding="utf-8")) if (corpus_dir / "corpus_stats.json").exists() else {}
    async with factory() as session:
        table = ModelVersion.__table__
        values = {
            "model_name": "tfidf_logreg",
            "model_version": model_version,
            "model_type": "sklearn_pipeline_tfidf_logreg",
            "artifact_path": str(artifact_path),
            "label_schema_version": "v1",
            "train_corpus_hash": stats.get("corpus_hash"),
            "train_size": stats.get("splits", {}).get("train"),
            "val_size": stats.get("splits", {}).get("val"),
            "test_size": stats.get("splits", {}).get("test"),
            "metrics": {},
            "confusion_matrix": {},
            "status": "candidate",
            "created_at": datetime.now(timezone.utc),
        }
        stmt = insert(table).values(**values)
        await session.execute(stmt.on_conflict_do_update(constraint="uq_model_versions_name_version", set_={key: stmt.excluded[key] for key in values if key != "created_at"}))
        await session.commit()
    return model_version


@app.command("train-candidate")
def train_candidate_command(corpus_dir: Path | None = typer.Option(None, "--corpus-dir", file_okay=False)) -> None:
    """Train a candidate TF-IDF LogisticRegression model from latest corpus."""

    version = run_async(train_candidate(corpus_dir))
    safe_echo(f"candidate_model_version={version}")


if __name__ == "__main__":
    app()
