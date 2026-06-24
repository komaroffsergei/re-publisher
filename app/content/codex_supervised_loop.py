from __future__ import annotations

import asyncio
import csv
import json
import shutil
import statistics
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from app.content.classifier import build_classification_text
from app.content.common import run_async, session_factory, settings_or_exit
from app.content.corpus_builder import stable_split
from app.content.link_enricher import enrich_pending
from app.content.local_summary import summarize_pending
from app.content.local_translation import translate_pending
from app.content.material_builder import build_new
from app.content.media_assets import download_link_images, register_telegram_media
from app.content.processor import process_new
from app.content.text_utils import clean_text
from app.content.url_extractor import extract_new
from app.content.yandex_genre_classifier import (
    FALLBACK_GENRE,
    GenreTaxonomy,
    clamp_float,
    clamp_int,
    genre_list_for_prompt,
    load_taxonomy,
)
from app.main import safe_echo
from app.models import (
    CodexGenreClassification,
    CodexGenreModelComparison,
    CodexTrainingRun,
    ContentItem,
    LinkSnapshot,
    ModelVersion,
    PostProcessed,
)

app = typer.Typer(no_args_is_help=True)

PROMPT_TEMPLATE_PATH = Path("config/codex_genre_axes_prompt.md")
MODEL_NAME = "tfidf_logreg"
AXIS_COLUMNS = ["difficulty_score", "promo_score", "opinion_score", "event_score"]
TEACHER_TEXT_CAP = 1600
TEACHER_SUMMARY_CAP = 900
CODEX_REQUIRED_KEYS = {
    "source_post_id",
    "content_item_id",
    "genre_primary",
    "genre_secondary",
    "genre_confidence",
    "difficulty_score",
    "promo_score",
    "opinion_score",
    "event_score",
    "needs_review",
    "reason",
}


@dataclass(frozen=True)
class TeacherBatch:
    run_id: str
    iteration: int
    batch_dir: Path
    input_path: Path
    output_path: Path
    exported_count: int
    local_media_count: int


@dataclass(frozen=True)
class SeriesSnapshot:
    series_id: str
    run_id: str
    run_index: int
    status: str
    run_labels: int
    cumulative_labels: int
    comparisons: int
    latest_match_percent: float | None
    avg_match_percent: float | None
    best_match_percent: float | None
    worst_match_percent: float | None
    exact_primary_percent: float | None
    secondary_overlap_percent: float | None
    multi_label_prediction_percent: float | None
    axis_mae: dict[str, float]
    delta_latest_match: float | None
    model_version: str | None
    report_path: str


@app.callback()
def main() -> None:
    """Codex-supervised genre-axis training loop."""


def artifacts_root(settings, run_id: str) -> Path:
    return Path(settings.artifacts_dir) / "codex_training" / run_id


def series_root(settings, series_id: str) -> Path:
    return Path(settings.artifacts_dir) / "codex_training_series" / series_id


def series_like(series_id: str) -> str:
    return f"{series_id}_run_%"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def axes_dict(row: Any) -> dict[str, int]:
    return {axis: int(getattr(row, axis)) for axis in AXIS_COLUMNS}


def split_for_post(source_post_id: int) -> str:
    split = stable_split(source_post_id)
    return "holdout" if split in {"val", "test"} else "train"


def is_media_only_unknown(item: ContentItem, processed: PostProcessed | None, snapshot: LinkSnapshot | None) -> bool:
    title = clean_text(item.translated_title or item.title or (snapshot.title if snapshot else None))
    source_summary = clean_text(item.translated_summary or item.source_summary or (snapshot.summary_short if snapshot else None))
    main_text = clean_text(item.main_text)
    if main_text or title or source_summary:
        return False
    return processed is None or bool(processed.has_media) or int(processed.word_count or 0) == 0


def clip_teacher_text(value: Any, limit: int) -> str:
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ..."


def teacher_input_row(item: ContentItem, processed: PostProcessed | None, snapshot: LinkSnapshot | None) -> dict[str, Any]:
    return {
        "source_post_id": item.source_post_id,
        "content_item_id": item.id,
        "title": clip_teacher_text(item.translated_title or item.title or (snapshot.title if snapshot else None), 240),
        "post_text": clip_teacher_text(item.main_text, TEACHER_TEXT_CAP),
        "source_summary": clip_teacher_text(
            item.translated_summary or item.source_summary or (snapshot.summary_short if snapshot else None),
            TEACHER_SUMMARY_CAP,
        ),
        "source_url": clean_text(item.source_url),
        "source_domain": clean_text(item.source_domain or (snapshot.domain if snapshot else None)),
        "flags": {
            "language": processed.language if processed else None,
            "word_count": processed.word_count if processed else None,
            "url_count": processed.url_count if processed else None,
            "domains": processed.domains if processed else [],
            "has_code": processed.has_code if processed else False,
            "has_github": processed.has_github if processed else False,
            "has_arxiv": processed.has_arxiv if processed else False,
            "has_media": processed.has_media if processed else False,
        },
    }


def local_media_teacher_row(item: ContentItem, taxonomy: GenreTaxonomy, run_id: str, iteration: int) -> dict[str, Any]:
    return {
        "source_post_id": item.source_post_id,
        "content_item_id": item.id,
        "run_id": run_id,
        "iteration": iteration,
        "taxonomy_version": taxonomy.version,
        "teacher_name": "local_media_prefilter",
        "split": "train_hard",
        "genre_primary": FALLBACK_GENRE,
        "genre_secondary": [],
        "genre_confidence": 1.0,
        "difficulty_score": 0,
        "promo_score": 0,
        "opinion_score": 0,
        "event_score": 0,
        "needs_review": True,
        "reason": "",
        "raw_response": {"local_rule": "media_only_or_empty_context"},
        "artifact_path": None,
        "created_at": now_utc(),
    }


def normalize_teacher_row(row: dict[str, Any], taxonomy: GenreTaxonomy) -> dict[str, Any] | None:
    allowed = set(taxonomy.genres)
    try:
        source_post_id = int(row["source_post_id"])
        content_item_id = int(row["content_item_id"])
    except (KeyError, TypeError, ValueError):
        return None
    primary = clean_text(row.get("genre_primary"))
    if primary not in allowed:
        primary = FALLBACK_GENRE
        needs_review = True
    else:
        needs_review = bool(row.get("needs_review"))
    secondary = []
    raw_secondary = row.get("genre_secondary") if isinstance(row.get("genre_secondary"), list) else []
    for value in raw_secondary:
        slug = clean_text(value)
        if slug in allowed and slug != primary and slug not in secondary:
            secondary.append(slug)
    return {
        "source_post_id": source_post_id,
        "content_item_id": content_item_id,
        "genre_primary": primary,
        "genre_secondary": secondary[:3],
        "genre_confidence": clamp_float(row.get("genre_confidence")),
        "difficulty_score": clamp_int(row.get("difficulty_score")),
        "promo_score": clamp_int(row.get("promo_score")),
        "opinion_score": clamp_int(row.get("opinion_score")),
        "event_score": clamp_int(row.get("event_score")),
        "needs_review": needs_review,
        "reason": clean_text(row.get("reason")),
        "raw_response": row,
    }


def local_heuristic_teacher(row: dict[str, Any], taxonomy: GenreTaxonomy) -> dict[str, Any]:
    text = " ".join(str(row.get(key) or "") for key in ["title", "post_text", "source_summary", "source_domain"]).lower()
    flags = row.get("flags") if isinstance(row.get("flags"), dict) else {}
    if not clean_text(row.get("title")) and not clean_text(row.get("post_text")) and not clean_text(row.get("source_summary")):
        primary = FALLBACK_GENRE
        confidence = 1.0
        needs_review = True
    elif any(token in text for token in ["ваканси", "hiring", "ищем", "career", "job"]):
        primary = "career_job"
        confidence = 0.72
        needs_review = False
    elif any(token in text for token in ["вебинар", "митап", "конференц", "webinar", "event", "запись"]):
        primary = "event_webinar"
        confidence = 0.72
        needs_review = False
    elif any(token in text for token in ["github", "библиотек", "инструмент", "tool", "api", "sdk"]):
        primary = "tool_product"
        confidence = 0.68
        needs_review = False
    elif bool(flags.get("has_arxiv")) or any(token in text for token in ["paper", "benchmark", "arxiv", "датасет", "модель"]):
        primary = "technical_research"
        confidence = 0.66
        needs_review = False
    elif any(token in text for token in ["рын", "выруч", "сделк", "инвест", "бизнес", "регулир"]):
        primary = "business_market"
        confidence = 0.65
        needs_review = False
    elif any(token in text for token in ["мем", "шут", "😂", "😁"]):
        primary = "humor_meme"
        confidence = 0.7
        needs_review = False
    elif "?" in str(row.get("post_text") or "") and len(str(row.get("post_text") or "")) < 500:
        primary = "community_chat"
        confidence = 0.62
        needs_review = True
    else:
        primary = "opinion_commentary"
        confidence = 0.58
        needs_review = True
    if primary not in taxonomy.genres:
        primary = FALLBACK_GENRE
        needs_review = True
    return {
        "source_post_id": row["source_post_id"],
        "content_item_id": row["content_item_id"],
        "genre_primary": primary,
        "genre_secondary": [],
        "genre_confidence": confidence,
        "difficulty_score": 4 if primary == "technical_research" else 3 if primary in {"tool_product", "business_market"} else 0,
        "promo_score": 4 if primary == "promo_ad" else 1 if primary in {"tool_product", "career_job", "event_webinar"} else 0,
        "opinion_score": 5 if primary == "opinion_commentary" else 2 if primary in {"community_chat", "business_market"} else 0,
        "event_score": 5 if primary == "event_webinar" else 0,
        "needs_review": needs_review,
        "reason": "local heuristic teacher fallback",
    }


async def run_backlog_pipeline(limit: int) -> dict[str, Any]:
    settings = settings_or_exit()
    results: dict[str, Any] = {
        "processed": await process_new(limit),
        "links": await extract_new(limit),
        "enriched": await enrich_pending(min(limit, 200)),
        "telegram_media": await register_telegram_media(min(limit, 200)),
        "link_images": await download_link_images(min(limit, 200)),
    }
    results["summaries"] = await summarize_pending(min(limit, 200))
    results["content_items"] = await build_new(limit)
    results["translations"] = await translate_pending(limit)
    return results


async def upsert_training_run(
    run_id: str,
    *,
    status: str,
    duration_hours: float,
    batch_size: int,
    report_interval_minutes: int,
    current_iteration: int = 0,
    metrics: dict[str, Any] | None = None,
    latest_model_version: str | None = None,
    latest_match_percent: float | None = None,
    best_match_percent: float | None = None,
    worst_match_percent: float | None = None,
    report_path: str | None = None,
    error: str | None = None,
    finished_at: datetime | None = None,
) -> None:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        values = {
            "run_id": run_id,
            "status": status,
            "duration_hours": duration_hours,
            "batch_size": batch_size,
            "report_interval_minutes": report_interval_minutes,
            "current_iteration": current_iteration,
            "metrics": metrics or {},
            "latest_model_version": latest_model_version,
            "latest_match_percent": latest_match_percent,
            "best_match_percent": best_match_percent,
            "worst_match_percent": worst_match_percent,
            "report_path": report_path,
            "error": error,
            "finished_at": finished_at,
            "updated_at": now_utc(),
        }
        table = CodexTrainingRun.__table__
        stmt = insert(table).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[table.c.run_id],
                set_={key: stmt.excluded[key] for key in values if key != "run_id"},
            )
        )
        await session.commit()


async def load_training_run_state(run_id: str) -> CodexTrainingRun | None:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        return (await session.execute(select(CodexTrainingRun).where(CodexTrainingRun.run_id == run_id))).scalar_one_or_none()


async def count_run_labels(run_id: str) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(CodexGenreClassification).where(CodexGenreClassification.run_id == run_id)
            )
        ).scalar_one()


async def count_series_labels(series_id: str) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(CodexGenreClassification)
                .where(CodexGenreClassification.run_id.like(series_like(series_id)))
            )
        ).scalar_one()


async def series_cursor_content_item_id(series_id: str) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        value = (
            await session.execute(
                select(func.max(CodexGenreClassification.content_item_id)).where(
                    CodexGenreClassification.run_id.like(series_like(series_id)),
                    CodexGenreClassification.content_item_id.is_not(None),
                )
            )
        ).scalar_one()
    return int(value or 0)


async def count_unlabeled_for_series(series_id: str) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    cursor_id = await series_cursor_content_item_id(series_id)
    async with factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(ContentItem)
                .join(PostProcessed, PostProcessed.post_id == ContentItem.source_post_id)
                .where(ContentItem.id > cursor_id)
            )
        ).scalar_one()


def training_goal_incomplete(
    *,
    label_count: int,
    best_match_percent: float | None,
    min_labels: int,
    target_match_percent: float | None,
) -> bool:
    if min_labels and label_count < min_labels:
        return True
    if target_match_percent is not None and (best_match_percent is None or best_match_percent < target_match_percent):
        return True
    return False


def previous_series_snapshot(root: Path) -> dict[str, Any] | None:
    stats_path = root / "series_stats.jsonl"
    if not stats_path.exists():
        return None
    last: dict[str, Any] | None = None
    for line in stats_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            last = json.loads(line)
    return last


async def store_teacher_rows(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        table = CodexGenreClassification.__table__
        for values in rows:
            stmt = insert(table).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_codex_genre_post_run",
                    set_={key: stmt.excluded[key] for key in values if key not in {"created_at"}},
                )
            )
        await session.commit()
    return len(rows)


async def export_teacher_batch(
    run_id: str,
    iteration: int,
    batch_size: int,
    *,
    exclude_series_id: str | None = None,
    min_content_item_id: int | None = None,
) -> TeacherBatch:
    settings = settings_or_exit()
    taxonomy = load_taxonomy()
    root = artifacts_root(settings, run_id) / "batches" / f"iter_{iteration:04d}"
    root.mkdir(parents=True, exist_ok=True)
    input_path = root / "input.jsonl"
    output_path = root / "codex_output.jsonl"
    if input_path.exists() and not output_path.exists():
        exported_count = len([line for line in input_path.read_text(encoding="utf-8").splitlines() if line.strip()])
        return TeacherBatch(
            run_id=run_id,
            iteration=iteration,
            batch_dir=root,
            input_path=input_path,
            output_path=output_path,
            exported_count=exported_count,
            local_media_count=0,
        )
    local_rows: list[dict[str, Any]] = []
    exported: list[dict[str, Any]] = []
    factory = session_factory(settings)
    async with factory() as session:
        stmt = (
            select(ContentItem, PostProcessed, LinkSnapshot)
            .join(PostProcessed, PostProcessed.post_id == ContentItem.source_post_id)
            .outerjoin(LinkSnapshot, LinkSnapshot.id == ContentItem.primary_snapshot_id)
            .outerjoin(
                CodexGenreClassification,
                (CodexGenreClassification.source_post_id == ContentItem.source_post_id)
                & (CodexGenreClassification.run_id == run_id),
            )
            .where(CodexGenreClassification.id.is_(None))
            .order_by(ContentItem.id)
            .limit(batch_size)
        )
        if min_content_item_id is not None:
            stmt = stmt.where(ContentItem.id > min_content_item_id)
        elif exclude_series_id and await count_series_labels(exclude_series_id) > 0:
            classified_alias = aliased(CodexGenreClassification)
            series_classified = (
                select(classified_alias.id)
                .where(
                    classified_alias.source_post_id == ContentItem.source_post_id,
                    classified_alias.run_id.like(series_like(exclude_series_id)),
                )
                .correlate(ContentItem)
                .exists()
            )
            stmt = stmt.where(~series_classified)
        result = await session.execute(stmt)
        for item, processed, snapshot in result.all():
            if is_media_only_unknown(item, processed, snapshot):
                local_rows.append(local_media_teacher_row(item, taxonomy, run_id, iteration))
            else:
                exported.append(teacher_input_row(item, processed, snapshot))
    await store_teacher_rows(local_rows)
    with input_path.open("w", encoding="utf-8") as handle:
        for row in exported:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return TeacherBatch(
        run_id=run_id,
        iteration=iteration,
        batch_dir=root,
        input_path=input_path,
        output_path=output_path,
        exported_count=len(exported),
        local_media_count=len(local_rows),
    )


def build_codex_prompt(batch: TeacherBatch) -> str:
    taxonomy = load_taxonomy()
    input_jsonl = batch.input_path.read_text(encoding="utf-8")
    expected_count = len([line for line in input_jsonl.splitlines() if line.strip()])
    return f"""You are the Codex teacher labeler for a Telegram AI-content corpus.

Use only your own reasoning in this Codex run. Do not call YandexGPT, web APIs, shell commands, tools, or files.
The taxonomy and batch records are provided inline below.

Return only JSONL in your final answer: exactly {expected_count} lines, one compact JSON object per input line.
Do not return markdown, explanations, code fences, headings, or prose outside JSONL.

Allowed genres, taxonomy_version={taxonomy.version}:
{genre_list_for_prompt(taxonomy)}

Axes:
- difficulty_score: {taxonomy.axes["difficulty_score"]}
- promo_score: {taxonomy.axes["promo_score"]}
- opinion_score: {taxonomy.axes["opinion_score"]}
- event_score: {taxonomy.axes["event_score"]}

For each input JSON line, return:
{{"source_post_id":123,"content_item_id":456,"genre_primary":"tool_product","genre_secondary":[],"genre_confidence":0.82,"difficulty_score":3,"promo_score":1,"opinion_score":0,"event_score":0,"needs_review":false,"reason":"short reason or empty"}}

Rules:
- Use source_post_id and content_item_id exactly as provided.
- Use only allowed genre slugs.
- Scores must be integers 0..5.
- genre_confidence must be 0..1.
- genre_secondary must be 0-3 allowed slugs and must not duplicate genre_primary.
- Do not invent facts not present in title, post_text, source_summary, source_url, source_domain, or flags.
- If text is empty, media-only, too short, or ambiguous, use media_only_unknown and needs_review=true.
- Prefer conservative confidence.

Input JSONL:
{input_jsonl}
"""


def read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        brace = text.find("{", index)
        if brace < 0:
            break
        try:
            parsed, end = decoder.raw_decode(text[brace:])
        except json.JSONDecodeError:
            index = brace + 1
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
        index = brace + max(end, 1)
    return rows


def validate_and_write_codex_output(batch: TeacherBatch, response_text: str) -> int:
    expected_rows = read_jsonl_objects(batch.input_path)
    expected_keys = [(int(row["source_post_id"]), int(row["content_item_id"])) for row in expected_rows]
    raw_rows = extract_json_objects(response_text)
    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for row in raw_rows:
        if not CODEX_REQUIRED_KEYS.issubset(row):
            continue
        try:
            key = (int(row["source_post_id"]), int(row["content_item_id"]))
        except (TypeError, ValueError):
            continue
        if key in expected_keys and key not in by_key:
            by_key[key] = row
    missing = [key for key in expected_keys if key not in by_key]
    if missing:
        raise ValueError(
            f"Codex output is incomplete for iteration {batch.iteration}: "
            f"expected={len(expected_keys)} parsed={len(by_key)} missing={missing[:5]}"
        )
    with batch.output_path.open("w", encoding="utf-8") as handle:
        for key in expected_keys:
            handle.write(json.dumps(by_key[key], ensure_ascii=False, sort_keys=True) + "\n")
    return len(expected_keys)


def run_codex_cli(batch: TeacherBatch, timeout_seconds: int = 1800) -> dict[str, Any]:
    prompt = build_codex_prompt(batch)
    stdout_path = batch.batch_dir / "codex.stdout.log"
    stderr_path = batch.batch_dir / "codex.stderr.log"
    last_message_path = batch.batch_dir / "codex.last_message.txt"
    command = [
        "codex",
        "exec",
        "--cd",
        str(Path.cwd()),
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--output-last-message",
        str(last_message_path),
        "-",
    ]
    started = now_utc()
    completed = subprocess.run(
        command,
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
    )
    stdout_path.write_text(completed.stdout or "", encoding="utf-8")
    stderr_path.write_text(completed.stderr or "", encoding="utf-8")
    parsed_count = 0
    parse_error = None
    response_text = last_message_path.read_text(encoding="utf-8") if last_message_path.exists() else completed.stdout
    try:
        parsed_count = validate_and_write_codex_output(batch, response_text)
    except Exception as exc:
        parse_error = str(exc)
    return {
        "returncode": completed.returncode,
        "started_at": started.isoformat(),
        "finished_at": now_utc().isoformat(),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "last_message_path": str(last_message_path),
        "output_path": str(batch.output_path),
        "parsed_count": parsed_count,
        "parse_error": parse_error,
    }


async def import_teacher_output(batch: TeacherBatch, backend: str = "codex_cli") -> int:
    settings = settings_or_exit()
    taxonomy = load_taxonomy()
    rows: list[dict[str, Any]] = []
    if backend == "local_heuristic":
        source_lines = batch.input_path.read_text(encoding="utf-8").splitlines() if batch.input_path.exists() else []
        parsed_source = [json.loads(line) for line in source_lines if line.strip()]
        raw_rows = [local_heuristic_teacher(row, taxonomy) for row in parsed_source]
    else:
        if not batch.output_path.exists():
            raise FileNotFoundError(f"Codex output not found: {batch.output_path}")
        raw_rows = [json.loads(line) for line in batch.output_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for raw in raw_rows:
        normalized = normalize_teacher_row(raw, taxonomy)
        if not normalized:
            continue
        split = split_for_post(normalized["source_post_id"])
        rows.append(
            {
                **normalized,
                "run_id": batch.run_id,
                "iteration": batch.iteration,
                "taxonomy_version": taxonomy.version,
                "teacher_name": backend,
                "split": split,
                "artifact_path": str(batch.output_path),
                "created_at": now_utc(),
            }
        )
    count = await store_teacher_rows(rows)
    manifest = {"imported": count, "backend": backend, "output_path": str(batch.output_path), "updated_at": now_utc().isoformat()}
    (batch.batch_dir / "import_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return count


async def collect_labeled_rows(
    run_id: str,
    include_holdout: bool = False,
    *,
    run_prefix: str | None = None,
) -> list[tuple[CodexGenreClassification, ContentItem, PostProcessed, LinkSnapshot | None]]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        stmt = (
            select(CodexGenreClassification, ContentItem, PostProcessed, LinkSnapshot)
            .join(ContentItem, ContentItem.id == CodexGenreClassification.content_item_id)
            .join(PostProcessed, PostProcessed.post_id == ContentItem.source_post_id)
            .outerjoin(LinkSnapshot, LinkSnapshot.id == ContentItem.primary_snapshot_id)
            .order_by(CodexGenreClassification.id)
        )
        if run_prefix:
            stmt = stmt.where(CodexGenreClassification.run_id.like(f"{run_prefix}%"))
        else:
            stmt = stmt.where(CodexGenreClassification.run_id == run_id)
        if not include_holdout:
            stmt = stmt.where(CodexGenreClassification.split.in_(["train", "train_hard"]))
        result = await session.execute(stmt)
        return list(result.all())


def training_text(item: ContentItem, processed: PostProcessed, snapshot: LinkSnapshot | None) -> str:
    return build_classification_text(
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


async def hard_example_scores(run_id: str, *, run_prefix: str | None = None) -> dict[int, float]:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        stmt = (
            select(CodexGenreModelComparison.source_post_id, func.min(CodexGenreModelComparison.match_percent))
            .group_by(CodexGenreModelComparison.source_post_id)
        )
        if run_prefix:
            stmt = stmt.where(CodexGenreModelComparison.run_id.like(f"{run_prefix}%"))
        else:
            stmt = stmt.where(CodexGenreModelComparison.run_id == run_id)
        result = await session.execute(stmt)
    return {int(source_post_id): float(score) for source_post_id, score in result.all() if score is not None}


def hard_example_weight(label: CodexGenreClassification, hard_scores: dict[int, float]) -> int:
    score = hard_scores.get(label.source_post_id)
    weight = 1
    if label.split == "train_hard":
        weight = max(weight, 2)
    if label.genre_primary == FALLBACK_GENRE:
        weight = max(weight, 4)
    if score is None:
        return weight
    if score < 50:
        return max(weight, 8)
    if score < 70:
        return max(weight, 5)
    if score < 90:
        return max(weight, 3)
    return weight


def make_classifier_pipeline(labels: list[Any]):
    from sklearn.dummy import DummyClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline

    unique = {str(label) for label in labels}
    estimator = DummyClassifier(strategy="most_frequent") if len(unique) < 2 else LogisticRegression(max_iter=2000, class_weight="balanced")
    return Pipeline(
        [
            ("tfidf", TfidfVectorizer(lowercase=True, ngram_range=(1, 2), min_df=1, max_features=100_000, sublinear_tf=True)),
            ("clf", estimator),
        ]
    )


def normalize_genre_set(primary: Any, secondary: Any) -> list[str]:
    values: list[str] = []
    for value in [primary, *(secondary if isinstance(secondary, list) else [])]:
        label = clean_text(value)
        if label and label not in values:
            values.append(label)
    return values


def make_multilabel_genre_model(texts: list[str], label_sets: list[list[str]]) -> dict[str, Any] | None:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.multiclass import OneVsRestClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import MultiLabelBinarizer

    mlb = MultiLabelBinarizer()
    y = mlb.fit_transform(label_sets)
    if len(mlb.classes_) < 2:
        return None
    model = Pipeline(
        [
            ("tfidf", TfidfVectorizer(lowercase=True, ngram_range=(1, 2), min_df=1, max_features=100_000, sublinear_tf=True)),
            ("clf", OneVsRestClassifier(LogisticRegression(max_iter=2000, class_weight="balanced"))),
        ]
    )
    model.fit(texts, y)
    return {"model": model, "binarizer": mlb}


def train_bundle_from_records(records: list[dict[str, Any]], model_dir: Path, run_id: str, iteration: int) -> tuple[Path, dict[str, Any]]:
    from joblib import dump
    import pandas as pd

    if len(records) < 2:
        raise ValueError("At least two training records are required.")
    data = pd.DataFrame(records)
    model_dir.mkdir(parents=True, exist_ok=True)
    bundle: dict[str, Any] = {
        "kind": "codex_supervised_genre_axes_bundle_v1",
        "run_id": run_id,
        "iteration": iteration,
        "genre_model": make_classifier_pipeline(list(data["genre_primary"])),
        "genre_multilabel": None,
        "axis_models": {},
        "axis_columns": AXIS_COLUMNS,
    }
    bundle["genre_model"].fit(data["text"].astype(str), data["genre_primary"].astype(str))
    label_sets = [
        normalize_genre_set(row["genre_primary"], row.get("genre_secondary") if isinstance(row.get("genre_secondary"), list) else [])
        for row in records
    ]
    bundle["genre_multilabel"] = make_multilabel_genre_model(list(data["text"].astype(str)), label_sets)
    for axis in AXIS_COLUMNS:
        model = make_classifier_pipeline(list(data[axis]))
        model.fit(data["text"].astype(str), data[axis].astype(int))
        bundle["axis_models"][axis] = model
    artifact_path = model_dir / "tfidf_logreg.joblib"
    dump(bundle, artifact_path)
    metadata = {
        "run_id": run_id,
        "iteration": iteration,
        "train_rows": int(len(data)),
        "labels": dict(Counter(data["genre_primary"])),
        "secondary_labels": dict(Counter(label for labels in label_sets for label in labels[1:])),
        "multilabel_enabled": bundle["genre_multilabel"] is not None,
        "created_at": now_utc().isoformat(),
    }
    (model_dir / "training_config.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact_path, metadata


async def train_candidate(run_id: str, iteration: int, *, train_run_prefix: str | None = None) -> tuple[str | None, dict[str, Any]]:
    settings = settings_or_exit()
    rows = await collect_labeled_rows(run_id, include_holdout=False, run_prefix=train_run_prefix)
    hard_scores = await hard_example_scores(run_id, run_prefix=train_run_prefix)
    records: list[dict[str, Any]] = []
    original_rows = 0
    oversampled_rows = 0
    weight_distribution: Counter[int] = Counter()
    for label, item, processed, snapshot in rows:
        text = training_text(item, processed, snapshot)
        if not text.strip():
            continue
        original_rows += 1
        weight = hard_example_weight(label, hard_scores)
        weight_distribution[weight] += 1
        record = {
            "source_post_id": label.source_post_id,
            "text": text,
            "genre_primary": label.genre_primary,
            "genre_secondary": list(label.genre_secondary or []),
            **axes_dict(label),
        }
        for _ in range(weight):
            records.append(dict(record))
        oversampled_rows += max(0, weight - 1)
    if len(records) < 2:
        return None, {"skipped": "not_enough_training_rows", "train_rows": len(records)}
    model_version = f"codex_axes_{run_id}_i{iteration:04d}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    model_dir = Path("models") / "candidates" / model_version
    artifact_path, metadata = train_bundle_from_records(records, model_dir, run_id, iteration)
    metadata["train_run_prefix"] = train_run_prefix
    metadata["original_train_rows"] = original_rows
    metadata["effective_train_rows"] = len(records)
    metadata["oversampled_rows"] = oversampled_rows
    metadata["hard_weight_distribution"] = {str(key): value for key, value in sorted(weight_distribution.items())}
    table_values = {
        "model_name": MODEL_NAME,
        "model_version": model_version,
        "model_type": "sklearn_bundle_tfidf_logreg_codex_genre_axes",
        "artifact_path": str(artifact_path),
        "label_schema_version": "yandex_axes_v1",
        "train_corpus_hash": train_run_prefix or run_id,
        "train_size": int(metadata["train_rows"]),
        "val_size": None,
        "test_size": None,
        "metrics": {"training": metadata},
        "confusion_matrix": {},
        "status": "candidate",
        "created_at": now_utc(),
    }
    factory = session_factory(settings)
    async with factory() as session:
        table = ModelVersion.__table__
        stmt = insert(table).values(**table_values)
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_model_versions_name_version",
                set_={key: stmt.excluded[key] for key in table_values if key != "created_at"},
            )
        )
        await session.commit()
    return model_version, metadata


def predict_bundle(bundle: dict[str, Any], text: str) -> dict[str, Any]:
    genre_model = bundle["genre_model"]
    predicted = str(genre_model.predict([text])[0])
    scores: dict[str, float] = {}
    secondary: list[str] = []
    if hasattr(genre_model, "predict_proba"):
        labels = list(getattr(genre_model, "classes_", []))
        proba = genre_model.predict_proba([text])[0]
        scores = {str(label): float(score) for label, score in zip(labels, proba)}
        secondary = [label for label, score in sorted(scores.items(), key=lambda item: item[1], reverse=True) if label != predicted and score >= 0.20][:3]
    multilabel = bundle.get("genre_multilabel")
    if multilabel:
        ml_model = multilabel["model"]
        mlb = multilabel["binarizer"]
        if hasattr(ml_model, "predict_proba"):
            ml_proba = ml_model.predict_proba([text])[0]
        else:
            ml_proba = ml_model.predict([text])[0]
        multilabel_scores = {str(label): float(score) for label, score in zip(mlb.classes_, ml_proba)}
        scores = {**scores, **{f"multi:{label}": score for label, score in multilabel_scores.items()}}
        ranked = [(label, score) for label, score in sorted(multilabel_scores.items(), key=lambda item: item[1], reverse=True) if label != predicted]
        selected = [label for label, score in ranked if score >= 0.25][:3]
        if not selected and ranked and ranked[0][1] >= 0.15:
            selected = [ranked[0][0]]
        if selected:
            secondary = selected
    axes = {axis: int(bundle["axis_models"][axis].predict([text])[0]) for axis in AXIS_COLUMNS}
    return {"genre_primary": predicted, "genre_secondary": secondary, "label_scores": scores, "axes": axes}


def media_only_prediction() -> dict[str, Any]:
    return {
        "genre_primary": FALLBACK_GENRE,
        "genre_secondary": [],
        "label_scores": {FALLBACK_GENRE: 1.0, "local_rule:media_only_unknown": 1.0},
        "axes": {axis: 0 for axis in AXIS_COLUMNS},
    }


def match_percent(
    teacher_genre: str,
    teacher_secondary: list[str],
    teacher_axes: dict[str, int],
    predicted_genre: str,
    predicted_secondary: list[str],
    predicted_axes: dict[str, int],
) -> tuple[float, list[str]]:
    if predicted_genre == teacher_genre:
        genre_score = 1.0
    elif predicted_genre in teacher_secondary or teacher_genre in predicted_secondary:
        genre_score = 0.5
    else:
        genre_score = 0.0
    axis_scores = [1 - (abs(int(predicted_axes.get(axis, 0)) - int(teacher_axes.get(axis, 0))) / 5) for axis in AXIS_COLUMNS]
    score = (genre_score * 0.6 + (sum(axis_scores) / len(axis_scores)) * 0.4) * 100
    flags: list[str] = []
    if genre_score < 1.0:
        flags.append("genre_mismatch" if genre_score == 0 else "genre_secondary_match")
    for axis in AXIS_COLUMNS:
        if int(predicted_axes.get(axis, 0)) != int(teacher_axes.get(axis, 0)):
            flags.append(f"{axis}_mismatch")
    return round(score, 2), flags


async def evaluate_candidate(
    run_id: str,
    iteration: int,
    model_version: str,
    evaluation_scope: str = "iteration",
    *,
    evaluation_run_prefix: str | None = None,
) -> dict[str, Any]:
    from joblib import load

    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        model_row = (
            await session.execute(select(ModelVersion).where(ModelVersion.model_name == MODEL_NAME, ModelVersion.model_version == model_version))
        ).scalar_one()
        bundle = load(model_row.artifact_path)
        stmt = (
            select(CodexGenreClassification, ContentItem, PostProcessed, LinkSnapshot)
            .join(ContentItem, ContentItem.id == CodexGenreClassification.content_item_id)
            .join(PostProcessed, PostProcessed.post_id == ContentItem.source_post_id)
            .outerjoin(LinkSnapshot, LinkSnapshot.id == ContentItem.primary_snapshot_id)
            .order_by(CodexGenreClassification.id)
        )
        if evaluation_run_prefix:
            stmt = stmt.where(CodexGenreClassification.run_id.like(f"{evaluation_run_prefix}%"))
        else:
            stmt = stmt.where(CodexGenreClassification.run_id == run_id)
        if evaluation_scope not in {"all", "series"}:
            stmt = stmt.where(CodexGenreClassification.iteration == iteration)
        result = await session.execute(stmt)
        rows = list(result.all())
        table = CodexGenreModelComparison.__table__
        scores: list[float] = []
        comparison_values: list[dict[str, Any]] = []
        for teacher, item, processed, snapshot in rows:
            text = training_text(item, processed, snapshot)
            prediction = media_only_prediction() if is_media_only_unknown(item, processed, snapshot) else predict_bundle(bundle, text)
            teacher_axes = axes_dict(teacher)
            percent, flags = match_percent(
                teacher.genre_primary,
                list(teacher.genre_secondary or []),
                teacher_axes,
                prediction["genre_primary"],
                prediction["genre_secondary"],
                prediction["axes"],
            )
            scores.append(percent)
            values = {
                "source_post_id": teacher.source_post_id,
                "content_item_id": teacher.content_item_id,
                "run_id": run_id,
                "iteration": iteration,
                "model_name": MODEL_NAME,
                "model_version": model_version,
                "split": teacher.split,
                "teacher_genre": teacher.genre_primary,
                "teacher_secondary": list(teacher.genre_secondary or []),
                "teacher_axes": teacher_axes,
                "predicted_genre": prediction["genre_primary"],
                "predicted_secondary": prediction["genre_secondary"],
                "predicted_axes": prediction["axes"],
                "label_scores": prediction["label_scores"],
                "match_percent": percent,
                "mismatch_flags": flags,
                "created_at": now_utc(),
            }
            comparison_values.append(values)
            stmt = insert(table).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_codex_comparison_post_model_run",
                    set_={key: stmt.excluded[key] for key in values if key != "created_at"},
                )
            )
        await session.commit()
    if not scores:
        return {"evaluated": 0, "latest_match_percent": None, "best_match_percent": None, "worst_match_percent": None}
    holdout_scores = [float(row["match_percent"]) for row in comparison_values if row["split"] == "holdout"]
    metric_scores = holdout_scores or scores
    return {
        "evaluated": len(scores),
        "latest_match_percent": round(statistics.mean(metric_scores), 2),
        "best_match_percent": round(max(scores), 2),
        "worst_match_percent": round(min(scores), 2),
        "holdout_count": len(holdout_scores),
        "train_count": len(scores) - len(holdout_scores),
        "evaluation_scope": evaluation_scope,
    }


async def harden_mismatches(run_id: str, iteration: int, threshold: float = 90.0) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        result = await session.execute(
            select(CodexGenreModelComparison.source_post_id)
            .where(
                CodexGenreModelComparison.run_id == run_id,
                CodexGenreModelComparison.iteration == iteration,
                CodexGenreModelComparison.match_percent < threshold,
            )
        )
        post_ids = [row[0] for row in result.all()]
        if not post_ids:
            return 0
        await session.execute(
            update(CodexGenreClassification)
            .where(CodexGenreClassification.run_id == run_id, CodexGenreClassification.source_post_id.in_(post_ids))
            .values(split="train_hard")
        )
        await session.commit()
        return len(post_ids)


async def write_report(run_id: str) -> Path:
    settings = settings_or_exit()
    root = artifacts_root(settings, run_id)
    report_dir = root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    factory = session_factory(settings)
    async with factory() as session:
        run = (await session.execute(select(CodexTrainingRun).where(CodexTrainingRun.run_id == run_id))).scalar_one_or_none()
        label_count = (await session.execute(select(func.count()).select_from(CodexGenreClassification).where(CodexGenreClassification.run_id == run_id))).scalar_one()
        comparison_count = (await session.execute(select(func.count()).select_from(CodexGenreModelComparison).where(CodexGenreModelComparison.run_id == run_id))).scalar_one()
        distribution_rows = await session.execute(
            select(CodexGenreClassification.genre_primary, func.count())
            .where(CodexGenreClassification.run_id == run_id)
            .group_by(CodexGenreClassification.genre_primary)
            .order_by(func.count().desc())
        )
        latest_rows = await session.execute(
            select(CodexGenreModelComparison)
            .where(CodexGenreModelComparison.run_id == run_id)
            .order_by(CodexGenreModelComparison.created_at.desc())
            .limit(5)
        )
        distribution = dict(distribution_rows.all())
        latest = list(latest_rows.scalars())
    path = report_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        f"# Codex Training Run {run_id}",
        "",
        f"- status: {run.status if run else 'unknown'}",
        f"- iteration: {run.current_iteration if run else 0}",
        f"- labels: {label_count}",
        f"- comparisons: {comparison_count}",
        f"- latest_match_percent: {float(run.latest_match_percent):.2f}" if run and run.latest_match_percent is not None else "- latest_match_percent: n/a",
        f"- best_match_percent: {float(run.best_match_percent):.2f}" if run and run.best_match_percent is not None else "- best_match_percent: n/a",
        f"- worst_match_percent: {float(run.worst_match_percent):.2f}" if run and run.worst_match_percent is not None else "- worst_match_percent: n/a",
        "",
        "## Genre Distribution",
        "",
    ]
    for genre, count in distribution.items():
        lines.append(f"- {genre}: {count}")
    lines.extend(["", "## Latest Comparisons", ""])
    for row in latest:
        lines.append(f"- post={row.source_post_id} teacher={row.teacher_genre} predicted={row.predicted_genre} match={float(row.match_percent):.2f}% flags={','.join(row.mismatch_flags or [])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def write_series_snapshot(series_id: str, run_id: str, run_index: int, status: str) -> SeriesSnapshot:
    settings = settings_or_exit()
    root = series_root(settings, series_id)
    report_dir = root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    previous = previous_series_snapshot(root)
    factory = session_factory(settings)
    async with factory() as session:
        run = (await session.execute(select(CodexTrainingRun).where(CodexTrainingRun.run_id == run_id))).scalar_one_or_none()
        run_labels = (
            await session.execute(
                select(func.count()).select_from(CodexGenreClassification).where(CodexGenreClassification.run_id == run_id)
            )
        ).scalar_one()
        cumulative_labels = (
            await session.execute(
                select(func.count())
                .select_from(CodexGenreClassification)
                .where(CodexGenreClassification.run_id.like(series_like(series_id)))
            )
        ).scalar_one()
        model_version = run.latest_model_version if run else None
        comparison_stmt = select(CodexGenreModelComparison).where(CodexGenreModelComparison.run_id == run_id)
        if model_version:
            comparison_stmt = comparison_stmt.where(CodexGenreModelComparison.model_version == model_version)
        comparison_rows = list((await session.execute(comparison_stmt)).scalars())

    comparisons = len(comparison_rows)
    match_values = [float(row.match_percent) for row in comparison_rows]
    exact_count = 0
    overlap_count = 0
    multi_pred_count = 0
    axis_abs_errors: dict[str, list[int]] = {axis: [] for axis in AXIS_COLUMNS}
    for row in comparison_rows:
        teacher_labels = {row.teacher_genre, *list(row.teacher_secondary or [])}
        predicted_labels = {row.predicted_genre, *list(row.predicted_secondary or [])}
        if row.predicted_genre == row.teacher_genre:
            exact_count += 1
        if teacher_labels & predicted_labels:
            overlap_count += 1
        if row.predicted_secondary:
            multi_pred_count += 1
        teacher_axes = row.teacher_axes or {}
        predicted_axes = row.predicted_axes or {}
        for axis in AXIS_COLUMNS:
            axis_abs_errors[axis].append(abs(int(predicted_axes.get(axis, 0)) - int(teacher_axes.get(axis, 0))))

    avg_match = round(statistics.mean(match_values), 2) if match_values else None
    previous_match = previous.get("avg_match_percent") if previous else None
    delta = round(avg_match - float(previous_match), 2) if avg_match is not None and previous_match is not None else None
    axis_mae = {
        axis: round(statistics.mean(values), 3) if values else 0.0
        for axis, values in axis_abs_errors.items()
    }
    snapshot = SeriesSnapshot(
        series_id=series_id,
        run_id=run_id,
        run_index=run_index,
        status=status,
        run_labels=int(run_labels),
        cumulative_labels=int(cumulative_labels),
        comparisons=comparisons,
        latest_match_percent=float(run.latest_match_percent) if run and run.latest_match_percent is not None else avg_match,
        avg_match_percent=avg_match,
        best_match_percent=round(max(match_values), 2) if match_values else None,
        worst_match_percent=round(min(match_values), 2) if match_values else None,
        exact_primary_percent=round(exact_count / comparisons * 100, 2) if comparisons else None,
        secondary_overlap_percent=round(overlap_count / comparisons * 100, 2) if comparisons else None,
        multi_label_prediction_percent=round(multi_pred_count / comparisons * 100, 2) if comparisons else None,
        axis_mae=axis_mae,
        delta_latest_match=delta,
        model_version=model_version,
        report_path=str(report_dir / f"run_{run_index:04d}_quality.md"),
    )
    data = snapshot.__dict__
    json_path = report_dir / f"run_{run_index:04d}_quality.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with (root / "series_stats.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        f"# Codex Training Series {series_id} Run {run_index:04d}",
        "",
        f"- run_id: {run_id}",
        f"- status: {status}",
        f"- run_labels: {snapshot.run_labels}",
        f"- cumulative_labels: {snapshot.cumulative_labels}",
        f"- comparisons: {snapshot.comparisons}",
        f"- latest_match_percent: {snapshot.latest_match_percent if snapshot.latest_match_percent is not None else 'n/a'}",
        f"- avg_match_percent: {snapshot.avg_match_percent if snapshot.avg_match_percent is not None else 'n/a'}",
        f"- delta_avg_match_percent: {snapshot.delta_latest_match if snapshot.delta_latest_match is not None else 'n/a'}",
        f"- exact_primary_percent: {snapshot.exact_primary_percent if snapshot.exact_primary_percent is not None else 'n/a'}",
        f"- secondary_overlap_percent: {snapshot.secondary_overlap_percent if snapshot.secondary_overlap_percent is not None else 'n/a'}",
        f"- multi_label_prediction_percent: {snapshot.multi_label_prediction_percent if snapshot.multi_label_prediction_percent is not None else 'n/a'}",
        f"- model_version: {snapshot.model_version or 'n/a'}",
        "",
        "## Axis MAE",
        "",
    ]
    for axis, value in snapshot.axis_mae.items():
        lines.append(f"- {axis}: {value}")
    Path(snapshot.report_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    async with factory() as session:
        if run:
            metrics = dict(run.metrics or {})
            metrics["series_id"] = series_id
            metrics["series_snapshot"] = data
            await session.execute(
                update(CodexTrainingRun)
                .where(CodexTrainingRun.run_id == run_id)
                .values(metrics=metrics, report_path=snapshot.report_path, updated_at=now_utc())
            )
            await session.commit()
    return snapshot


async def run_iteration(
    run_id: str,
    iteration: int,
    batch_size: int,
    teacher_backend: str,
    evaluation_scope: str = "iteration",
    *,
    exclude_series_id: str | None = None,
    train_run_prefix: str | None = None,
    evaluation_run_prefix: str | None = None,
    run_pipeline: bool = True,
    min_content_item_id: int | None = None,
) -> dict[str, Any]:
    settings = settings_or_exit()
    existing_batch_dir = artifacts_root(settings, run_id) / "batches" / f"iter_{iteration:04d}"
    has_reusable_teacher_input = (existing_batch_dir / "input.jsonl").exists() and not (existing_batch_dir / "codex_output.jsonl").exists()
    if has_reusable_teacher_input:
        pipeline = {"skipped": "reusing_existing_teacher_input"}
    elif not run_pipeline:
        pipeline = {"skipped": "disabled_for_scheduled_series"}
    else:
        pipeline = await run_backlog_pipeline(max(batch_size * 4, batch_size))
    batch = await export_teacher_batch(
        run_id,
        iteration,
        batch_size,
        exclude_series_id=exclude_series_id,
        min_content_item_id=min_content_item_id,
    )
    codex_result: dict[str, Any] = {}
    imported = 0
    if batch.exported_count:
        if teacher_backend == "codex_cli":
            for attempt in range(1, 3):
                codex_result = await asyncio.to_thread(run_codex_cli, batch)
                codex_result["attempt"] = attempt
                if (
                    codex_result.get("returncode") == 0
                    and batch.output_path.exists()
                    and not codex_result.get("parse_error")
                ):
                    break
                if attempt == 2:
                    raise RuntimeError(f"codex exec failed or produced invalid output: {codex_result}")
        imported = await import_teacher_output(batch, backend=teacher_backend)
    model_version, training = await train_candidate(run_id, iteration, train_run_prefix=train_run_prefix)
    evaluation: dict[str, Any] = {"evaluated": 0}
    hardened = 0
    if model_version:
        evaluation = await evaluate_candidate(
            run_id,
            iteration,
            model_version,
            evaluation_scope=evaluation_scope,
            evaluation_run_prefix=evaluation_run_prefix,
        )
        hardened = await harden_mismatches(run_id, iteration)
    total_labels = await count_run_labels(run_id)
    cumulative_labels = await count_series_labels(exclude_series_id) if exclude_series_id else total_labels
    report_path = await write_report(run_id)
    return {
        "pipeline": pipeline,
        "exported": batch.exported_count,
        "local_media": batch.local_media_count,
        "imported": imported,
        "codex": codex_result,
        "model_version": model_version,
        "training": training,
        "evaluation": evaluation,
        "hardened_mismatches": hardened,
        "total_labels": total_labels,
        "cumulative_labels": cumulative_labels,
        "report_path": str(report_path),
    }


async def run_loop(
    run_id: str,
    duration_hours: float,
    batch_size: int,
    report_interval_minutes: int,
    teacher_backend: str,
    min_labels: int,
    target_match_percent: float | None,
    evaluation_scope: str,
) -> None:
    started = time.monotonic()
    deadline = started + duration_hours * 3600
    existing_run = await load_training_run_state(run_id)
    best: float | None = float(existing_run.best_match_percent) if existing_run and existing_run.best_match_percent is not None else None
    worst: float | None = float(existing_run.worst_match_percent) if existing_run and existing_run.worst_match_percent is not None else None
    latest: float | None = float(existing_run.latest_match_percent) if existing_run and existing_run.latest_match_percent is not None else None
    iteration = int(existing_run.current_iteration or 0) if existing_run else 0
    if existing_run and existing_run.status == "failed" and iteration > 0:
        iteration -= 1
    await upsert_training_run(
        run_id,
        status="running",
        duration_hours=duration_hours,
        batch_size=batch_size,
        report_interval_minutes=report_interval_minutes,
        current_iteration=iteration,
        metrics={
            "min_labels": min_labels,
            "target_match_percent": target_match_percent,
            "evaluation_scope": evaluation_scope,
            "resumed_from_status": existing_run.status if existing_run else None,
        },
        latest_match_percent=latest,
        best_match_percent=best,
        worst_match_percent=worst,
        report_path=existing_run.report_path if existing_run else None,
    )
    last_report = started
    try:
        label_count = await count_run_labels(run_id)
        while time.monotonic() < deadline or training_goal_incomplete(
            label_count=label_count,
            best_match_percent=best,
            min_labels=min_labels,
            target_match_percent=target_match_percent,
        ):
            iteration += 1
            result = await run_iteration(run_id, iteration, batch_size, teacher_backend, evaluation_scope=evaluation_scope)
            evaluation = result.get("evaluation") or {}
            latest = evaluation.get("latest_match_percent")
            if latest is not None:
                best = float(latest) if best is None else max(best, float(latest))
                worst = float(latest) if worst is None else min(worst, float(latest))
            label_count = int(result.get("total_labels") or label_count)
            report_path = result.get("report_path")
            metrics = {
                "last_iteration": result,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "total_labels": label_count,
                "min_labels": min_labels,
                "target_match_percent": target_match_percent,
                "evaluation_scope": evaluation_scope,
            }
            await upsert_training_run(
                run_id,
                status="running",
                duration_hours=duration_hours,
                batch_size=batch_size,
                report_interval_minutes=report_interval_minutes,
                current_iteration=iteration,
                metrics=metrics,
                latest_model_version=result.get("model_version"),
                latest_match_percent=latest,
                best_match_percent=best,
                worst_match_percent=worst,
                report_path=report_path,
            )
            safe_echo(
                f"iteration={iteration} labels={result.get('imported', 0) + result.get('local_media', 0)} total_labels={label_count} "
                f"model={result.get('model_version')} latest_match={latest} best={best} worst={worst}"
            )
            if time.monotonic() - last_report >= report_interval_minutes * 60:
                report = await write_report(run_id)
                last_report = time.monotonic()
                safe_echo(f"report={report}")
            if result.get("exported", 0) == 0 and result.get("local_media", 0) == 0:
                await asyncio.sleep(60)
    except Exception as exc:
        await upsert_training_run(
            run_id,
            status="failed",
            duration_hours=duration_hours,
            batch_size=batch_size,
            report_interval_minutes=report_interval_minutes,
            current_iteration=iteration,
            latest_match_percent=latest,
            best_match_percent=best,
            worst_match_percent=worst,
            error=str(exc),
            finished_at=now_utc(),
        )
        raise
    report = await write_report(run_id)
    await upsert_training_run(
        run_id,
        status="finished",
        duration_hours=duration_hours,
        batch_size=batch_size,
        report_interval_minutes=report_interval_minutes,
        current_iteration=iteration,
        latest_match_percent=latest,
        best_match_percent=best,
        worst_match_percent=worst,
        report_path=str(report),
        finished_at=now_utc(),
    )


async def run_series_chunk(
    series_id: str,
    run_index: int,
    *,
    chunk_size: int,
    batch_size: int,
    teacher_backend: str,
    evaluation_scope: str,
    run_pipeline: bool,
) -> SeriesSnapshot:
    run_id = f"{series_id}_run_{run_index:04d}"
    started = time.monotonic()
    run_prefix = f"{series_id}_run_"
    existing_run = await load_training_run_state(run_id)
    iteration = int(existing_run.current_iteration or 0) if existing_run else 0
    if existing_run and existing_run.status == "failed" and iteration > 0:
        iteration -= 1
    best: float | None = float(existing_run.best_match_percent) if existing_run and existing_run.best_match_percent is not None else None
    worst: float | None = float(existing_run.worst_match_percent) if existing_run and existing_run.worst_match_percent is not None else None
    latest: float | None = float(existing_run.latest_match_percent) if existing_run and existing_run.latest_match_percent is not None else None
    await upsert_training_run(
        run_id,
        status="running",
        duration_hours=0.0,
        batch_size=batch_size,
        report_interval_minutes=0,
        current_iteration=iteration,
        metrics={
            "series_id": series_id,
            "run_index": run_index,
            "chunk_size": chunk_size,
            "train_run_prefix": run_prefix,
            "evaluation_scope": evaluation_scope,
            "resumed_from_status": existing_run.status if existing_run else None,
        },
        latest_match_percent=latest,
        best_match_percent=best,
        worst_match_percent=worst,
        report_path=existing_run.report_path if existing_run else None,
    )
    run_label_count = await count_run_labels(run_id)
    status = "finished"
    try:
        while run_label_count < chunk_size:
            iteration += 1
            cursor_id = await series_cursor_content_item_id(series_id)
            result = await run_iteration(
                run_id,
                iteration,
                min(batch_size, chunk_size - run_label_count),
                teacher_backend,
                evaluation_scope=evaluation_scope,
                exclude_series_id=series_id,
                train_run_prefix=run_prefix,
                evaluation_run_prefix=run_prefix,
                run_pipeline=run_pipeline,
                min_content_item_id=cursor_id,
            )
            added = int(result.get("imported", 0) or 0) + int(result.get("local_media", 0) or 0)
            evaluation = result.get("evaluation") or {}
            latest = evaluation.get("latest_match_percent")
            if latest is not None:
                best = float(latest) if best is None else max(best, float(latest))
                worst = float(latest) if worst is None else min(worst, float(latest))
            run_label_count = await count_run_labels(run_id)
            metrics = {
                "series_id": series_id,
                "run_index": run_index,
                "chunk_size": chunk_size,
                "run_labels": run_label_count,
                "cumulative_labels": result.get("cumulative_labels"),
                "cursor_content_item_id": cursor_id,
                "train_run_prefix": run_prefix,
                "evaluation_scope": evaluation_scope,
                "last_iteration": result,
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
            await upsert_training_run(
                run_id,
                status="running",
                duration_hours=0.0,
                batch_size=batch_size,
                report_interval_minutes=0,
                current_iteration=iteration,
                metrics=metrics,
                latest_model_version=result.get("model_version"),
                latest_match_percent=latest,
                best_match_percent=best,
                worst_match_percent=worst,
                report_path=result.get("report_path"),
            )
            safe_echo(
                f"series={series_id} run={run_index} iteration={iteration} "
                f"added={added} run_labels={run_label_count}/{chunk_size} "
                f"cursor={cursor_id} cumulative_labels={result.get('cumulative_labels')} "
                f"latest_match={latest} best={best}"
            )
            if added == 0 and result.get("exported", 0) == 0 and result.get("local_media", 0) == 0:
                status = "finished_no_more_items"
                break
    except Exception as exc:
        await upsert_training_run(
            run_id,
            status="failed",
            duration_hours=0.0,
            batch_size=batch_size,
            report_interval_minutes=0,
            current_iteration=iteration,
            latest_match_percent=latest,
            best_match_percent=best,
            worst_match_percent=worst,
            error=str(exc),
            finished_at=now_utc(),
        )
        await write_series_snapshot(series_id, run_id, run_index, "failed")
        raise

    report = await write_report(run_id)
    await upsert_training_run(
        run_id,
        status=status,
        duration_hours=0.0,
        batch_size=batch_size,
        report_interval_minutes=0,
        current_iteration=iteration,
        latest_match_percent=latest,
        best_match_percent=best,
        worst_match_percent=worst,
        report_path=str(report),
        finished_at=now_utc(),
    )
    snapshot = await write_series_snapshot(series_id, run_id, run_index, status)
    safe_echo(
        f"series_snapshot={snapshot.report_path} run_labels={snapshot.run_labels} "
        f"cumulative_labels={snapshot.cumulative_labels} avg_match={snapshot.avg_match_percent} "
        f"delta={snapshot.delta_latest_match}"
    )
    return snapshot


async def run_scheduled_series(
    series_id: str,
    *,
    chunk_size: int,
    batch_size: int,
    interval_minutes: float,
    teacher_backend: str,
    evaluation_scope: str,
    max_runs: int,
    run_pipeline: bool,
) -> None:
    settings = settings_or_exit()
    root = series_root(settings, series_id)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    run_index = 1
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_index = int(manifest.get("next_run_index") or 1)
    completed = 0
    while True:
        cursor_id = await series_cursor_content_item_id(series_id)
        remaining = await count_unlabeled_for_series(series_id)
        if remaining <= 0:
            safe_echo(f"series={series_id} complete: no unlabeled content_items left")
            break
        if max_runs and completed >= max_runs:
            safe_echo(f"series={series_id} stopped: max_runs={max_runs}")
            break
        manifest = {
            "series_id": series_id,
            "chunk_size": chunk_size,
            "batch_size": batch_size,
            "interval_minutes": interval_minutes,
            "teacher_backend": teacher_backend,
            "evaluation_scope": evaluation_scope,
            "run_pipeline": run_pipeline,
            "next_run_index": run_index,
            "cursor_content_item_id": cursor_id,
            "remaining_before_next_run": remaining,
            "updated_at": now_utc().isoformat(),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        safe_echo(f"series={series_id} starting run={run_index} remaining={remaining}")
        snapshot = await run_series_chunk(
            series_id,
            run_index,
            chunk_size=chunk_size,
            batch_size=batch_size,
            teacher_backend=teacher_backend,
            evaluation_scope=evaluation_scope,
            run_pipeline=run_pipeline,
        )
        completed += 1
        run_index += 1
        cursor_after_run = await series_cursor_content_item_id(series_id)
        remaining_after_run = await count_unlabeled_for_series(series_id)
        manifest["next_run_index"] = run_index
        manifest["last_snapshot"] = snapshot.__dict__
        manifest["cursor_content_item_id"] = cursor_after_run
        manifest["remaining_after_run"] = remaining_after_run
        manifest["updated_at"] = now_utc().isoformat()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if snapshot.status == "finished_no_more_items":
            break
        if interval_minutes > 0:
            await asyncio.sleep(interval_minutes * 60)


@app.command("export-batch")
def export_batch_command(
    run_id: str = typer.Option(..., "--run-id"),
    iteration: int = typer.Option(1, "--iteration", min=1),
    batch_size: int = typer.Option(50, "--batch-size", min=1),
) -> None:
    batch = run_async(export_teacher_batch(run_id, iteration, batch_size))
    safe_echo(f"input={batch.input_path} output={batch.output_path} exported={batch.exported_count} local_media={batch.local_media_count}")


@app.command("run-once")
def run_once_command(
    run_id: str = typer.Option(..., "--run-id"),
    iteration: int = typer.Option(1, "--iteration", min=1),
    batch_size: int = typer.Option(50, "--batch-size", min=1),
    teacher_backend: str = typer.Option("codex_cli", "--teacher-backend"),
    evaluation_scope: str = typer.Option("iteration", "--evaluation-scope"),
) -> None:
    result = run_async(run_iteration(run_id, iteration, batch_size, teacher_backend, evaluation_scope=evaluation_scope))
    safe_echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@app.command("run")
def run_command(
    run_id: str = typer.Option(None, "--run-id"),
    duration_hours: float = typer.Option(8.0, "--duration-hours", min=0.01),
    batch_size: int = typer.Option(50, "--batch-size", min=1),
    report_interval_minutes: int = typer.Option(30, "--report-interval-minutes", min=1),
    teacher_backend: str = typer.Option("codex_cli", "--teacher-backend"),
    min_labels: int = typer.Option(0, "--min-labels", min=0),
    target_match_percent: float | None = typer.Option(None, "--target-match-percent", min=0.0, max=100.0),
    evaluation_scope: str = typer.Option("iteration", "--evaluation-scope"),
) -> None:
    actual_run_id = run_id or f"codex_axes_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    run_async(
        run_loop(
            actual_run_id,
            duration_hours,
            batch_size,
            report_interval_minutes,
            teacher_backend,
            min_labels,
            target_match_percent,
            evaluation_scope,
        )
    )
    safe_echo(f"run_id={actual_run_id}")


@app.command("scheduled-runs")
def scheduled_runs_command(
    series_id: str = typer.Option(None, "--series-id"),
    chunk_size: int = typer.Option(500, "--chunk-size", min=1),
    batch_size: int = typer.Option(50, "--batch-size", min=1),
    interval_minutes: float = typer.Option(5.0, "--interval-minutes", min=0.0),
    teacher_backend: str = typer.Option("codex_cli", "--teacher-backend"),
    evaluation_scope: str = typer.Option("series", "--evaluation-scope"),
    max_runs: int = typer.Option(0, "--max-runs", min=0),
    run_pipeline: bool = typer.Option(False, "--run-pipeline/--skip-pipeline"),
) -> None:
    actual_series_id = series_id or f"codex_axes_series_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    run_async(
        run_scheduled_series(
            actual_series_id,
            chunk_size=chunk_size,
            batch_size=batch_size,
            interval_minutes=interval_minutes,
            teacher_backend=teacher_backend,
            evaluation_scope=evaluation_scope,
            max_runs=max_runs,
            run_pipeline=run_pipeline,
        )
    )
    safe_echo(f"series_id={actual_series_id}")


@app.command("series-report")
def series_report_command(
    series_id: str = typer.Option(..., "--series-id"),
    run_index: int = typer.Option(0, "--run-index", min=0),
) -> None:
    if run_index <= 0:
        root = series_root(settings_or_exit(), series_id)
        previous = previous_series_snapshot(root)
        if not previous:
            raise typer.BadParameter(f"No snapshots found for series_id={series_id}")
        run_index = int(previous["run_index"])
        run_id = str(previous["run_id"])
    else:
        run_id = f"{series_id}_run_{run_index:04d}"
    snapshot = run_async(write_series_snapshot(series_id, run_id, run_index, "reported"))
    safe_echo(f"series_report={snapshot.report_path}")


@app.command("report")
def report_command(run_id: str = typer.Option(..., "--run-id")) -> None:
    path = run_async(write_report(run_id))
    safe_echo(f"report={path}")


if __name__ == "__main__":
    app()
