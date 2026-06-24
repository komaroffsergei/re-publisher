from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import typer
from sqlalchemy import select

from app.content.classifier import build_classification_text
from app.content.common import run_async, session_factory, settings_or_exit
from app.main import safe_echo
from app.models import ContentItem, LinkSnapshot, PostLabel, PostProcessed

app = typer.Typer(no_args_is_help=True)
DEFAULT_PACKAGE = Path(r"C:\Users\New\Downloads\telegram_labeled_corpus_package")


@app.callback()
def main() -> None:
    """Corpus builder commands."""


def stable_split(post_id: int | str) -> str:
    digest = int(hashlib.sha256(str(post_id).encode("utf-8")).hexdigest()[:8], 16) % 100
    if digest < 10:
        return "test"
    if digest < 20:
        return "val"
    return "train"


def corpus_hash(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: str(item.get("id"))):
        digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def read_initial_rows(package_dir: Path) -> list[dict]:
    source = package_dir / "telegram_posts_trainable_model_v1.csv"
    if not source.exists():
        return []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


async def db_label_rows() -> list[dict]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        result = await session.execute(
            select(PostLabel, ContentItem, PostProcessed, LinkSnapshot)
            .join(ContentItem, ContentItem.source_post_id == PostLabel.post_id)
            .join(PostProcessed, PostProcessed.post_id == PostLabel.post_id)
            .outerjoin(LinkSnapshot, LinkSnapshot.id == ContentItem.primary_snapshot_id)
            .where(PostLabel.status == "accepted")
        )
        rows: list[dict] = []
        for label, item, processed, snapshot in result.all():
            text = build_classification_text(
                processed.clean_text,
                snapshot.title if snapshot else item.title,
                snapshot.description if snapshot else None,
                item.translated_summary or item.source_summary,
                processed.domains,
                {"has_github": processed.has_github, "has_arxiv": processed.has_arxiv, "has_code": processed.has_code},
            )
            rows.append(
                {
                    "id": item.source_post_id,
                    "label": label.label,
                    "detailed_label": label.label,
                    "text": text,
                    "split": stable_split(item.source_post_id),
                    "label_confidence": label.confidence or 1.0,
                    "chat_title": "",
                    "date": "",
                    "topic_tags": "",
                    "secondary_labels": "",
                }
            )
        return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    columns = ["id", "label", "detailed_label", "text", "split", "label_confidence", "chat_title", "date", "topic_tags", "secondary_labels"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


async def build_corpus(package_dir: Path) -> Path:
    settings = settings_or_exit()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target = Path(settings.artifacts_dir) / "corpus" / run_id
    rows_by_id: dict[str, dict] = {str(row.get("id")): row for row in read_initial_rows(package_dir)}
    for row in await db_label_rows():
        rows_by_id[str(row["id"])] = row
    rows = list(rows_by_id.values())
    for split in ["train", "val", "test"]:
        write_csv(target / f"{split}.csv", [row for row in rows if row.get("split") == split])
    write_csv(target / "full.csv", rows)
    with (target / "full.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    stats = {
        "rows": len(rows),
        "splits": {split: sum(1 for row in rows if row.get("split") == split) for split in ["train", "val", "test"]},
        "labels": {},
        "corpus_hash": corpus_hash(rows),
    }
    for row in rows:
        stats["labels"][row["label"]] = stats["labels"].get(row["label"], 0) + 1
    (target / "corpus_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "corpus_report.md").write_text(
        "# Corpus Report\n\n"
        f"- Rows: {stats['rows']}\n"
        f"- Corpus hash: `{stats['corpus_hash']}`\n"
        f"- Splits: {stats['splits']}\n"
        f"- Labels: {stats['labels']}\n",
        encoding="utf-8",
    )
    return target


@app.command("build-corpus")
def build_corpus_command(package_dir: Path = typer.Option(DEFAULT_PACKAGE, "--package-dir", file_okay=False)) -> None:
    """Export stable train/val/test corpus files."""

    target = run_async(build_corpus(package_dir))
    safe_echo(f"corpus_dir={target}")


if __name__ == "__main__":
    app()
