from __future__ import annotations

import json
from pathlib import Path

import typer
from sqlalchemy import select, update

from app.content.common import run_async, session_factory, settings_or_exit
from app.main import safe_echo
from app.models import ModelVersion

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Model evaluation commands."""


async def latest_candidate(session) -> ModelVersion:
    result = await session.execute(
        select(ModelVersion).where(ModelVersion.model_name == "tfidf_logreg", ModelVersion.status == "candidate").order_by(ModelVersion.id.desc()).limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise RuntimeError("No candidate model found")
    return row


async def evaluate_candidate(model_version: str | None = None) -> dict:
    settings = settings_or_exit()
    import pandas as pd
    from joblib import load
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score

    factory = session_factory(settings)
    async with factory() as session:
        if model_version:
            result = await session.execute(select(ModelVersion).where(ModelVersion.model_name == "tfidf_logreg", ModelVersion.model_version == model_version))
            candidate = result.scalar_one()
        else:
            candidate = await latest_candidate(session)
        artifact = Path(candidate.artifact_path)
        config = json.loads((artifact.parent / "training_config.json").read_text(encoding="utf-8"))
        corpus_dir = Path(config["corpus_dir"])
        model = load(artifact)
        metrics: dict = {}
        matrices: dict = {}
        for split in ["val", "test"]:
            data = pd.read_csv(corpus_dir / f"{split}.csv").dropna(subset=["text", "label"])
            if data.empty:
                continue
            y_true = data["label"].astype(str)
            y_pred = model.predict(data["text"].astype(str))
            metrics[split] = {
                "accuracy": float(accuracy_score(y_true, y_pred)),
                "macro_f1": float(f1_score(y_true, y_pred, average="macro")),
                "weighted_f1": float(f1_score(y_true, y_pred, average="weighted")),
                "per_class": classification_report(y_true, y_pred, output_dict=True, zero_division=0),
                "class_distribution": y_true.value_counts().to_dict(),
            }
            matrices[split] = confusion_matrix(y_true, y_pred, labels=list(model.classes_)).tolist()
        report_path = artifact.parent / "evaluation_report.md"
        report_path.write_text(
            "# Evaluation Report\n\n"
            f"- Model version: `{candidate.model_version}`\n"
            f"- Metrics: `{json.dumps(metrics, ensure_ascii=False)}`\n",
            encoding="utf-8",
        )
        await session.execute(update(ModelVersion).where(ModelVersion.id == candidate.id).values(metrics=metrics, confusion_matrix=matrices))
        await session.commit()
        return metrics


@app.command("evaluate-candidate")
def evaluate_candidate_command(model_version: str | None = typer.Option(None, "--model-version")) -> None:
    """Evaluate a candidate model on frozen val/test splits."""

    metrics = run_async(evaluate_candidate(model_version))
    safe_echo(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
