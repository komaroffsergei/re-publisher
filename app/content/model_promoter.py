from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer
from sqlalchemy import select, update

from app.content.common import run_async, session_factory, settings_or_exit
from app.main import safe_echo
from app.models import ModelVersion

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Model promotion commands."""


def test_metrics(model: ModelVersion) -> dict:
    return (model.metrics or {}).get("test") or {}


def promotion_decision(candidate: ModelVersion, active: ModelVersion | None, settings) -> tuple[bool, str]:
    candidate_metrics = test_metrics(candidate)
    if not candidate_metrics:
        return False, "candidate_has_no_test_metrics"
    if not active:
        return True, "no_active_model"
    active_metrics = test_metrics(active)
    if not active_metrics:
        return False, "active_model_has_no_comparable_test_metrics"
    macro_ok = candidate_metrics.get("macro_f1", 0) >= active_metrics.get("macro_f1", 0) + settings.model_promotion_min_macro_f1_delta
    weighted_ok = candidate_metrics.get("weighted_f1", 0) >= active_metrics.get("weighted_f1", 0) + settings.model_promotion_min_weighted_f1_delta
    per_class = candidate_metrics.get("per_class") or {}
    class_ok = all(
        values.get("support", 0) < 3 or values.get("f1-score", 0) >= settings.model_promotion_min_class_f1
        for label, values in per_class.items()
        if isinstance(values, dict) and label not in {"accuracy", "macro avg", "weighted avg"}
    )
    if macro_ok and weighted_ok and class_ok:
        return True, "metric_gates_passed"
    return False, f"metric_gates_failed macro_ok={macro_ok} weighted_ok={weighted_ok} class_ok={class_ok}"


async def promote_if_better(model_version: str | None = None) -> str:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        candidate_query = select(ModelVersion).where(ModelVersion.model_name == "tfidf_logreg", ModelVersion.status == "candidate")
        if model_version:
            candidate_query = candidate_query.where(ModelVersion.model_version == model_version)
        candidate = (await session.execute(candidate_query.order_by(ModelVersion.id.desc()).limit(1))).scalar_one_or_none()
        if not candidate:
            return "no_candidate"
        active = (
            await session.execute(select(ModelVersion).where(ModelVersion.model_name == "tfidf_logreg", ModelVersion.status == "active").order_by(ModelVersion.id.desc()).limit(1))
        ).scalar_one_or_none()
        should_promote, reason = promotion_decision(candidate, active, settings)
        if should_promote:
            target = Path(settings.classifier_active_model_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate.artifact_path, target)
            await session.execute(update(ModelVersion).where(ModelVersion.model_name == "tfidf_logreg", ModelVersion.status == "active").values(status="archived"))
            await session.execute(update(ModelVersion).where(ModelVersion.id == candidate.id).values(status="active", artifact_path=str(target), promoted_at=datetime.now(timezone.utc)))
        else:
            metrics = candidate.metrics or {}
            metrics["promotion_decision"] = {"promoted": False, "reason": reason}
            await session.execute(update(ModelVersion).where(ModelVersion.id == candidate.id).values(status="rejected", metrics=metrics))
        await session.commit()
        return json.dumps({"promoted": should_promote, "reason": reason}, ensure_ascii=False)


@app.command("promote-if-better")
def promote_if_better_command(model_version: str | None = typer.Option(None, "--model-version")) -> None:
    """Promote a candidate only when metric gates pass."""

    safe_echo(run_async(promote_if_better(model_version)))


if __name__ == "__main__":
    app()
