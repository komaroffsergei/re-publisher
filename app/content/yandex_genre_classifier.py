from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import typer
import yaml
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.text_utils import clean_text
from app.content.yandex_gpt import (
    SUMMARY_MODEL_NAME,
    YandexGPTError,
    clip_prompt_text,
    complete,
    model_name_from_uri,
    model_uri,
    strip_json_fence,
)
from app.main import safe_echo
from app.models import ContentItem, LinkSnapshot, PostProcessed, YandexGenreClassification

app = typer.Typer(no_args_is_help=True)
DEFAULT_TAXONOMY_PATH = Path("config/yandexgpt_genre_axes.yaml")
FALLBACK_GENRE = "media_only_unknown"


@dataclass(frozen=True)
class GenreTaxonomy:
    version: str
    model: str
    genres: dict[str, dict[str, Any]]
    axes: dict[str, str]
    prompt: dict[str, str]


@dataclass(frozen=True)
class GenreResult:
    source_post_id: int
    content_item_id: int
    model_name: str
    taxonomy_version: str
    genre_primary: str
    genre_secondary: list[str]
    genre_confidence: float
    difficulty_score: int
    promo_score: int
    opinion_score: int
    event_score: int
    needs_review: bool
    reason: str
    raw_response: dict[str, Any]
    usage: dict[str, Any]


@app.callback()
def main() -> None:
    """YandexGPT Lite genre-axis classifier commands."""


def load_taxonomy(path: Path = DEFAULT_TAXONOMY_PATH) -> GenreTaxonomy:
    if not path.exists():
        raise YandexGPTError(f"Yandex genre taxonomy not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    genres = data.get("genres") or []
    genre_map = {str(item["slug"]): item for item in genres if isinstance(item, dict) and item.get("slug")}
    if len(genre_map) < 10:
        raise YandexGPTError("Yandex genre taxonomy must contain at least 10 genres.")
    if FALLBACK_GENRE not in genre_map:
        raise YandexGPTError(f"Yandex genre taxonomy must contain {FALLBACK_GENRE}.")
    axes = data.get("axes") or {}
    prompt = data.get("prompt") or {}
    required_axes = {"difficulty_score", "promo_score", "opinion_score", "event_score"}
    if required_axes - set(axes):
        raise YandexGPTError(f"Yandex genre taxonomy missing axes: {sorted(required_axes - set(axes))}")
    if not prompt.get("system") or not prompt.get("user"):
        raise YandexGPTError("Yandex genre taxonomy prompt is incomplete.")
    return GenreTaxonomy(
        version=str(data.get("version") or "yandex_axes_v1"),
        model=str(data.get("model") or SUMMARY_MODEL_NAME),
        genres=genre_map,
        axes={str(key): clean_text(value) for key, value in axes.items()},
        prompt={"system": clean_text(prompt["system"]), "user": clean_text(prompt["user"])},
    )


def genre_list_for_prompt(taxonomy: GenreTaxonomy) -> str:
    lines = []
    for slug, item in taxonomy.genres.items():
        hint = clean_text(item.get("hint") or item.get("title_ru") or item.get("description"))
        lines.append(f"{slug}: {hint}")
    return "\n".join(lines)


def build_flags(processed: PostProcessed | None) -> str:
    if not processed:
        return "нет"
    flags = {
        "language": processed.language,
        "word_count": processed.word_count,
        "url_count": processed.url_count,
        "domains": processed.domains,
        "has_code": processed.has_code,
        "has_github": processed.has_github,
        "has_arxiv": processed.has_arxiv,
        "has_media": processed.has_media,
    }
    return json.dumps(flags, ensure_ascii=False, sort_keys=True)


def build_genre_messages(
    taxonomy: GenreTaxonomy,
    *,
    item: ContentItem,
    processed: PostProcessed | None,
    snapshot: LinkSnapshot | None,
    include_reason: bool = False,
    max_input_chars: int = 900,
) -> tuple[str, str]:
    source_summary = clean_text(item.translated_summary or item.source_summary or (snapshot.summary_short if snapshot else None))
    title = clean_text(item.translated_title or item.title or (snapshot.title if snapshot else None))
    reason_rule = (
        "reason: коротко объясни решение на русском, особенно если needs_review=true."
        if include_reason
        else 'reason: верни пустую строку ""; объяснение не нужно.'
    )
    user = taxonomy.prompt["user"].format(
        genre_list=genre_list_for_prompt(taxonomy),
        difficulty_axis=taxonomy.axes["difficulty_score"],
        promo_axis=taxonomy.axes["promo_score"],
        opinion_axis=taxonomy.axes["opinion_score"],
        event_axis=taxonomy.axes["event_score"],
        reason_rule=reason_rule,
        title=title or "нет",
        post_text=clip_prompt_text(item.main_text, max_input_chars) or "нет",
        source_summary=clip_prompt_text(source_summary, max_input_chars) or "нет",
        source_url=clean_text(item.source_url) or "нет",
        source_domain=clean_text(item.source_domain) or "нет",
        flags=build_flags(processed),
    )
    return taxonomy.prompt["system"], user


def is_media_only_unknown(item: ContentItem, processed: PostProcessed | None, snapshot: LinkSnapshot | None) -> bool:
    title = clean_text(item.translated_title or item.title or (snapshot.title if snapshot else None))
    source_summary = clean_text(item.translated_summary or item.source_summary or (snapshot.summary_short if snapshot else None))
    main_text = clean_text(item.main_text)
    no_context = not main_text and not title and not source_summary
    if not no_context:
        return False
    if processed is None:
        return True
    return bool(processed.has_media) or int(processed.word_count or 0) == 0


def media_only_result(item: ContentItem, taxonomy: GenreTaxonomy) -> GenreResult:
    return GenreResult(
        source_post_id=item.source_post_id,
        content_item_id=item.id,
        model_name="local_media_prefilter",
        taxonomy_version=taxonomy.version,
        genre_primary=FALLBACK_GENRE,
        genre_secondary=[],
        genre_confidence=1.0,
        difficulty_score=0,
        promo_score=0,
        opinion_score=0,
        event_score=0,
        needs_review=True,
        reason="",
        raw_response={
            "genre_primary": FALLBACK_GENRE,
            "genre_secondary": [],
            "genre_confidence": 1.0,
            "difficulty_score": 0,
            "promo_score": 0,
            "opinion_score": 0,
            "event_score": 0,
            "needs_review": True,
            "reason": "",
            "local_rule": "media_only_or_empty_context",
        },
        usage={},
    )


def clamp_int(value: Any, *, default: int = 0, min_value: int = 0, max_value: int = 5) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def clamp_float(value: Any, *, default: float = 0.0, min_value: float = 0.0, max_value: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def usage_total_tokens(usage: dict[str, Any] | None) -> int:
    if not usage:
        return 0
    for key in ("totalTokens", "total_tokens", "total"):
        if key in usage:
            try:
                return int(usage[key])
            except (TypeError, ValueError):
                return 0
    total = 0
    for key in ("inputTextTokens", "completionTokens", "input_tokens", "completion_tokens"):
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def parse_genre_response(text: str, taxonomy: GenreTaxonomy) -> dict[str, Any]:
    try:
        data = json.loads(strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise YandexGPTError(f"Yandex genre JSON parse failed: {exc}") from exc
    if not isinstance(data, dict):
        raise YandexGPTError("Yandex genre response must be a JSON object.")

    allowed = set(taxonomy.genres)
    primary = clean_text(str(data.get("genre_primary") or ""))
    needs_review = bool(data.get("needs_review"))
    reason = clean_text(str(data.get("reason") or ""))
    if primary not in allowed:
        primary = FALLBACK_GENRE
        needs_review = True
        reason = clean_text(f"{reason} Некорректный жанр заменен на {FALLBACK_GENRE}.")

    secondary_source = data.get("genre_secondary") if isinstance(data.get("genre_secondary"), list) else []
    secondary = []
    for value in secondary_source:
        slug = clean_text(str(value))
        if slug in allowed and slug != primary and slug not in secondary:
            secondary.append(slug)
    return {
        "genre_primary": primary,
        "genre_secondary": secondary[:3],
        "genre_confidence": clamp_float(data.get("genre_confidence")),
        "difficulty_score": clamp_int(data.get("difficulty_score")),
        "promo_score": clamp_int(data.get("promo_score")),
        "opinion_score": clamp_int(data.get("opinion_score")),
        "event_score": clamp_int(data.get("event_score")),
        "needs_review": needs_review,
        "reason": reason,
        "raw_response": data,
    }


def result_to_json(result: GenreResult, run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "source_post_id": result.source_post_id,
        "content_item_id": result.content_item_id,
        "model_name": result.model_name,
        "taxonomy_version": result.taxonomy_version,
        "genre_primary": result.genre_primary,
        "genre_secondary": result.genre_secondary,
        "genre_confidence": result.genre_confidence,
        "difficulty_score": result.difficulty_score,
        "promo_score": result.promo_score,
        "opinion_score": result.opinion_score,
        "event_score": result.event_score,
        "needs_review": result.needs_review,
        "reason": result.reason,
        "raw_response": result.raw_response,
        "usage": result.usage,
    }


def write_artifacts(settings, run_id: str, results: list[GenreResult], cumulative_tokens: int) -> Path:
    target = Path(settings.artifacts_dir) / "yandex_genre" / run_id
    target.mkdir(parents=True, exist_ok=True)
    jsonl_path = target / "results.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result_to_json(result, run_id), ensure_ascii=False, sort_keys=True) + "\n")

    distribution = Counter(result.genre_primary for result in results)
    reviewed = sum(1 for result in results if result.needs_review)
    report_lines = [
        f"# Yandex Genre Run {run_id}",
        "",
        f"- rows: {len(results)}",
        f"- cumulative_total_tokens: {cumulative_tokens}",
        f"- needs_review: {reviewed}",
        "",
        "## Genre Distribution",
        "",
    ]
    for genre, count in distribution.most_common():
        report_lines.append(f"- {genre}: {count}")
    if results:
        report_lines.extend(
            [
                "",
                "## Axis Averages",
                "",
                f"- difficulty_score: {sum(r.difficulty_score for r in results) / len(results):.2f}",
                f"- promo_score: {sum(r.promo_score for r in results) / len(results):.2f}",
                f"- opinion_score: {sum(r.opinion_score for r in results) / len(results):.2f}",
                f"- event_score: {sum(r.event_score for r in results) / len(results):.2f}",
            ]
        )
    (target / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return target


async def classify_items(
    limit: int,
    write: bool,
    run_id: str | None,
    dry_run: bool,
    include_reason: bool = False,
    max_input_chars: int = 900,
) -> tuple[list[GenreResult], int, Path, int, int]:
    settings = settings_or_exit()
    taxonomy = load_taxonomy()
    actual_run_id = run_id or f"yandex_axes_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    model = model_uri(settings, "summary")
    factory = session_factory(settings)
    cumulative_tokens = 0
    api_calls = 0
    skipped_media_only = 0
    results: list[GenreResult] = []
    async with factory() as session:
        result = await session.execute(
            select(ContentItem, PostProcessed, LinkSnapshot)
            .join(PostProcessed, PostProcessed.post_id == ContentItem.source_post_id)
            .outerjoin(LinkSnapshot, LinkSnapshot.id == ContentItem.primary_snapshot_id)
            .order_by(ContentItem.id)
            .limit(limit)
        )
        rows = list(result.all())
        table = YandexGenreClassification.__table__
        for index, (item, processed, snapshot) in enumerate(rows, start=1):
            if is_media_only_unknown(item, processed, snapshot):
                skipped_media_only += 1
                genre_result = media_only_result(item, taxonomy)
            else:
                system_prompt, user_prompt = build_genre_messages(
                    taxonomy,
                    item=item,
                    processed=processed,
                    snapshot=snapshot,
                    include_reason=include_reason,
                    max_input_chars=max_input_chars,
                )
                completion = await complete(
                    settings,
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.0,
                    max_tokens=260 if include_reason else 180,
                )
                api_calls += 1
                parsed = parse_genre_response(completion.text, taxonomy)
                model_name = model_name_from_uri(completion.model_uri)
                genre_result = GenreResult(
                    source_post_id=item.source_post_id,
                    content_item_id=item.id,
                    model_name=model_name,
                    taxonomy_version=taxonomy.version,
                    genre_primary=parsed["genre_primary"],
                    genre_secondary=parsed["genre_secondary"],
                    genre_confidence=parsed["genre_confidence"],
                    difficulty_score=parsed["difficulty_score"],
                    promo_score=parsed["promo_score"],
                    opinion_score=parsed["opinion_score"],
                    event_score=parsed["event_score"],
                    needs_review=parsed["needs_review"],
                    reason=parsed["reason"],
                    raw_response=parsed["raw_response"],
                    usage=completion.usage,
                )
            results.append(genre_result)
            last_tokens = usage_total_tokens(genre_result.usage)
            cumulative_tokens += last_tokens
            safe_echo(
                "processed="
                f"{index}/{len(rows)} "
                f"source_post_id={item.source_post_id} "
                f"genre={genre_result.genre_primary} "
                f"difficulty={genre_result.difficulty_score} "
                f"promo={genre_result.promo_score} "
                f"opinion={genre_result.opinion_score} "
                f"event={genre_result.event_score} "
                f"last_total_tokens={last_tokens} "
                f"cumulative_total_tokens={cumulative_tokens} "
                f"api_calls={api_calls} "
                f"skipped_media_only={skipped_media_only}"
            )
            if write and not dry_run:
                values = result_to_json(genre_result, actual_run_id)
                values.pop("run_id")
                values["run_id"] = actual_run_id
                values["created_at"] = datetime.now(timezone.utc)
                stmt = insert(table).values(**values)
                await session.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_yandex_genre_post_run",
                        set_={key: stmt.excluded[key] for key in values if key not in {"source_post_id", "run_id", "created_at"}},
                    )
                )
        if write and not dry_run:
            await session.commit()
    artifact_dir = write_artifacts(settings, actual_run_id, results, cumulative_tokens)
    return results, cumulative_tokens, artifact_dir, api_calls, skipped_media_only


async def report_run(run_id: str) -> str:
    settings = settings_or_exit()
    factory = session_factory(settings)
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(YandexGenreClassification).where(YandexGenreClassification.run_id == run_id).order_by(YandexGenreClassification.id)
                )
            ).scalars()
        )
        if not rows:
            return f"run_id={run_id} rows=0"
        distribution = Counter(row.genre_primary for row in rows)
        total_tokens = sum(usage_total_tokens(row.usage) for row in rows)
        avg_difficulty = sum(row.difficulty_score for row in rows) / len(rows)
        avg_promo = sum(row.promo_score for row in rows) / len(rows)
        avg_opinion = sum(row.opinion_score for row in rows) / len(rows)
        avg_event = sum(row.event_score for row in rows) / len(rows)
        needs_review = sum(1 for row in rows if row.needs_review)
        distribution_text = ", ".join(f"{genre}={count}" for genre, count in distribution.most_common())
        return (
            f"run_id={run_id} rows={len(rows)} total_tokens={total_tokens} needs_review={needs_review} "
            f"avg_difficulty={avg_difficulty:.2f} avg_promo={avg_promo:.2f} "
            f"avg_opinion={avg_opinion:.2f} avg_event={avg_event:.2f} distribution={distribution_text}"
        )


@app.command("classify")
def classify_command(
    limit: int = limit_option(3),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not write database rows. Artifacts are still written."),
    write: bool = typer.Option(False, "--write", help="Write rows to yandex_genre_classifications."),
    run_id: str | None = typer.Option(None, "--run-id", help="Stable run identifier for DB rows and artifacts."),
    include_reason: bool = typer.Option(False, "--include-reason", help="Ask YandexGPT to include a short reason. Costs more tokens."),
    max_input_chars: int = typer.Option(900, "--max-input-chars", min=200, max=4000, help="Maximum post/summary characters sent to YandexGPT."),
) -> None:
    """Classify content items with YandexGPT Lite genre-axis taxonomy."""

    if dry_run and write:
        raise typer.BadParameter("--dry-run and --write cannot be used together.")
    results, total_tokens, artifact_dir, api_calls, skipped_media_only = run_async(
        classify_items(
            limit=limit,
            write=write,
            run_id=run_id,
            dry_run=dry_run,
            include_reason=include_reason,
            max_input_chars=max_input_chars,
        )
    )
    safe_echo(
        f"rows={len(results)} api_calls={api_calls} skipped_media_only={skipped_media_only} "
        f"cumulative_total_tokens={total_tokens} artifacts={artifact_dir}"
    )


@app.command("report")
def report_command(run_id: str = typer.Option(..., "--run-id", help="Run identifier to summarize.")) -> None:
    """Print aggregate stats for a stored YandexGPT genre run."""

    safe_echo(run_async(report_run(run_id)))


if __name__ == "__main__":
    app()
