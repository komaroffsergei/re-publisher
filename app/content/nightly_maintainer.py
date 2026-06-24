from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import typer
import yaml
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.content.classifier import classify_new
from app.content.common import run_async, session_factory, settings_or_exit
from app.content.corpus_builder import build_corpus
from app.content.link_enricher import enrich_pending
from app.content.local_summary import summarize_pending
from app.content.local_translation import translate_pending
from app.content.material_builder import build_new
from app.content.media_assets import download_link_images, register_telegram_media
from app.content.processor import process_new
from app.content.router import route_new
from app.content.rewriter import rewrite_pending
from app.content.search import reindex
from app.content.url_extractor import extract_new
from app.content.validators import validate_pending
from app.main import safe_echo
from app.models import ContentItem, LabelingQueue, PostClassification, PostLabel, PostProcessed

app = typer.Typer(no_args_is_help=True)


async def run_nightly(limit: int = 1000) -> Path:
    settings = settings_or_exit()
    results: dict[str, int | str] = {}
    results["processed"] = await process_new(limit)
    results["links"] = await extract_new(limit)
    results["enriched"] = await enrich_pending(min(limit, 200))
    results["telegram_media"] = await register_telegram_media(min(limit, 200))
    results["link_images"] = await download_link_images(min(limit, 200))
    results["summaries"] = await summarize_pending(min(limit, 200))
    results["content_items"] = await build_new(limit)
    results["translations"] = await translate_pending(limit)
    results["classifications"] = await classify_new(limit)
    results["routes"] = await route_new(limit)
    results["drafts"] = await rewrite_pending(limit)
    results["validated"] = await validate_pending(limit)
    results["search_documents"] = await reindex(limit)

    factory = session_factory(settings)
    async with factory() as session:
        total_processed = (await session.execute(select(func.count()).select_from(PostProcessed))).scalar_one()
        low_conf = (await session.execute(select(func.count()).select_from(LabelingQueue).where(LabelingQueue.status == "pending"))).scalar_one()
        distribution_rows = await session.execute(select(PostClassification.label_primary, func.count()).group_by(PostClassification.label_primary))
        distribution = {label or "none": count for label, count in distribution_rows.all()}
    report_dir = Path(settings.reports_dir) / "nightly"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{date.today().isoformat()}.md"
    report_path.write_text(
        "# Nightly Pipeline Report\n\n"
        f"- Generated at: {datetime.now(timezone.utc).isoformat()}\n"
        f"- Stage counts: `{json.dumps(results, ensure_ascii=False)}`\n"
        f"- Total processed posts: {total_processed}\n"
        f"- Classification distribution: `{json.dumps(distribution, ensure_ascii=False)}`\n"
        f"- Low-confidence pending queue: {low_conf}\n"
        "- Promotion decision: not run automatically unless enough accepted labels and explicit training steps are scheduled.\n",
        encoding="utf-8",
    )
    return report_path


async def export_codex_labeling_batch(limit: int = 200) -> Path:
    settings = settings_or_exit()
    target = Path(settings.artifacts_dir) / "codex_labels" / date.today().isoformat()
    target.mkdir(parents=True, exist_ok=True)
    factory = session_factory(settings)
    async with factory() as session:
        result = await session.execute(
            select(LabelingQueue, PostProcessed, ContentItem)
            .join(PostProcessed, PostProcessed.post_id == LabelingQueue.post_id)
            .outerjoin(ContentItem, ContentItem.source_post_id == LabelingQueue.post_id)
            .where(LabelingQueue.status == "pending")
            .order_by(LabelingQueue.id)
            .limit(limit)
        )
        with (target / "input_posts.jsonl").open("w", encoding="utf-8") as handle:
            for queue, processed, item in result.all():
                handle.write(
                    json.dumps(
                        {
                            "post_id": queue.post_id,
                            "text": processed.clean_text,
                            "title": item.title if item else None,
                            "summary": item.source_summary if item else None,
                            "suggested_label": queue.suggested_label,
                            "suggested_confidence": float(queue.suggested_confidence or 0),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    (target / "taxonomy_proposal.yaml").write_text("proposals: []\n", encoding="utf-8")
    (target / "codex_labeling_report.md").write_text("# Codex Labeling Report\n\nPending Codex run.\n", encoding="utf-8")
    return target


def schema_labels() -> set[str]:
    path = Path("config/label_schema.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {item["slug"] for item in data.get("labels", [])}


async def import_codex_labels(path: Path) -> int:
    settings = settings_or_exit()
    labels = schema_labels()
    count = 0
    factory = session_factory(settings)
    async with factory() as session:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                label = row.get("proposed_label")
                if label not in labels:
                    continue
                confidence = float(row.get("confidence") or 0)
                status = "accepted" if settings.auto_accept_codex_labels and confidence >= 0.90 and not row.get("needs_human_review") else "proposed"
                await session.execute(
                    insert(PostLabel.__table__).values(
                        post_id=int(row["post_id"]),
                        label=label,
                        label_set_version="v1",
                        confidence=confidence,
                        source="codex_agent",
                        status=status,
                        created_by="codex_nightly",
                        raw=row,
                    )
                )
                count += 1
        await session.commit()
    return count


@app.command("run")
def run_command(limit: int = typer.Option(1000, "--limit", min=1)) -> None:
    """Run the local nightly editorial pipeline."""

    safe_echo(f"report={run_async(run_nightly(limit))}")


@app.command("export-codex-labeling-batch")
def export_codex_labeling_batch_command(limit: int = typer.Option(200, "--limit", min=1)) -> None:
    """Export low-confidence posts for optional Codex labeling."""

    safe_echo(f"codex_batch_dir={run_async(export_codex_labeling_batch(limit))}")


@app.command("import-codex-labels")
def import_codex_labels_command(path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Import validated Codex JSONL label proposals as proposed labels."""

    safe_echo(f"imported={run_async(import_codex_labels(path))}")


if __name__ == "__main__":
    app()
