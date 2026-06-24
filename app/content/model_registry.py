from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from app.content.common import run_async, session_factory, settings_or_exit
from app.main import safe_echo
from app.models import ModelVersion

app = typer.Typer(no_args_is_help=True)

DEFAULT_PACKAGE = Path(r"C:\Users\New\Downloads\telegram_labeled_corpus_package")


@app.callback()
def main() -> None:
    """Model registry commands."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def register_initial_model(package_dir: Path, model_version: str) -> Path:
    settings = settings_or_exit()
    source_model = package_dir / "tfidf_logreg_model_full_corpus.joblib"
    source_meta = package_dir / "full_corpus_model_metadata.json"
    if not source_model.exists():
        raise FileNotFoundError(source_model)
    target_model = Path(settings.classifier_active_model_path)
    target_model.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_model, target_model)
    if source_meta.exists():
        shutil.copy2(source_meta, target_model.parent / "full_corpus_model_metadata.json")
        metadata = json.loads(source_meta.read_text(encoding="utf-8"))
    else:
        metadata = {}

    factory = session_factory(settings)
    async with factory() as session:
        await session.execute(
            update(ModelVersion)
            .where(ModelVersion.model_name == "tfidf_logreg", ModelVersion.status == "active")
            .values(status="archived")
        )
        table = ModelVersion.__table__
        values = {
            "model_name": "tfidf_logreg",
            "model_version": model_version,
            "model_type": "sklearn_pipeline_tfidf_logreg",
            "artifact_path": str(target_model),
            "label_schema_version": "v1",
            "train_corpus_hash": metadata.get("source_sha256") or sha256_file(source_model),
            "train_size": metadata.get("split_distribution", {}).get("train") if isinstance(metadata.get("split_distribution"), dict) else metadata.get("total_training_rows"),
            "val_size": metadata.get("split_distribution", {}).get("val") if isinstance(metadata.get("split_distribution"), dict) else None,
            "test_size": metadata.get("split_distribution", {}).get("test") if isinstance(metadata.get("split_distribution"), dict) else None,
            "metrics": {"imported_metadata": metadata},
            "confusion_matrix": {},
            "status": "active",
            "promoted_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
        }
        stmt = insert(table).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_model_versions_name_version",
                set_={key: stmt.excluded[key] for key in values if key not in {"created_at"}},
            )
        )
        await session.commit()
    return target_model


@app.command("register-initial-model")
def register_initial_model_command(
    package_dir: Path = typer.Option(DEFAULT_PACKAGE, "--package-dir", exists=True, file_okay=False),
    model_version: str = typer.Option("full_corpus_v1", "--model-version"),
) -> None:
    """Copy the prepared corpus model into models/active and mark it active."""

    target = run_async(register_initial_model(package_dir, model_version))
    safe_echo(f"active_model={target}")


if __name__ == "__main__":
    app()
