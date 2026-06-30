from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer
from sqlalchemy import select

from app.config import Settings
from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.link_materials import ARTICLE_LIKE_URL_TYPES
from app.content.pipeline_logic import READY_DRAFT_STATUS
from app.models import (
    ContentItem,
    LinkSnapshot,
    MediaAsset,
    PipelineEntry,
    PostLink,
    PublicationDraft,
    TelegramChat,
    TelegramPost,
)

app = typer.Typer(no_args_is_help=True)

NOISE_GENRES = {"media_only_unknown", "community_chat", "promo_ad"}
OPTIONAL_MANUAL_TOPICS = {"ai_events_jobs", "ai_humor_lab"}
BUSINESS_KEYWORDS = {
    "enterprise",
    "startup",
    "founder",
    "ceo",
    "cto",
    "invest",
    "market",
    "business",
    "revenue",
    "customer",
    "productivity",
    "бизнес",
    "рынок",
    "стартап",
    "инвест",
    "выруч",
    "компан",
    "внедрен",
    "продуктив",
    "клиент",
    "стратег",
    "корпорат",
}
STOP_WORDS = {
    "https",
    "http",
    "www",
    "com",
    "для",
    "или",
    "как",
    "что",
    "это",
    "уже",
    "ещё",
    "еще",
    "при",
    "про",
    "без",
    "над",
    "под",
    "после",
    "from",
    "with",
    "this",
    "that",
    "have",
    "will",
    "about",
    "into",
    "your",
    "their",
    "they",
    "them",
    "using",
    "нов",
    "мож",
    "сам",
    "the",
    "and",
    "for",
    "you",
    "are",
    "was",
    "were",
}


@dataclass(frozen=True)
class TopicDefinition:
    slug: str
    title: str
    description: str
    channel_idea: str
    mode: str


TOPIC_DEFINITIONS: dict[str, TopicDefinition] = {
    "ai_research_engineering": TopicDefinition(
        slug="ai_research_engineering",
        title="ИИ Исследования и инженерия",
        description="Технические статьи, research notes, архитектуры, evals, практическая инженерия LLM/ML.",
        channel_idea="Канал для инженеров и техлидов: коротко о новых методах, моделях, бенчмарках и production-подходах.",
        mode="auto_candidate",
    ),
    "ai_tools_products": TopicDefinition(
        slug="ai_tools_products",
        title="ИИ Инструменты",
        description="Новые AI-продукты, open-source инструменты, сервисы, библиотеки, workflow-автоматизация.",
        channel_idea="Канал-каталог полезных инструментов с техническим разбором, где и зачем применять.",
        mode="auto_candidate",
    ),
    "ai_business_strategy": TopicDefinition(
        slug="ai_business_strategy",
        title="ИИ Бизнес и стратегия",
        description="Внедрение ИИ в компаниях, рынки, кейсы, инвестиции, продуктовая стратегия.",
        channel_idea="Канал для руководителей и продуктовых команд: что меняется в бизнесе из-за ИИ.",
        mode="auto_candidate",
    ),
    "ai_news_monitor": TopicDefinition(
        slug="ai_news_monitor",
        title="ИИ Новости",
        description="Анонсы моделей, релизы, регуляторика, важные события без рекламного шума.",
        channel_idea="Оперативная лента важных AI-новостей с кратким техническим контекстом.",
        mode="auto_candidate",
    ),
    "ai_events_jobs": TopicDefinition(
        slug="ai_events_jobs",
        title="ИИ События и карьера",
        description="Вебинары, конференции, вакансии и карьерные объявления в AI.",
        channel_idea="Отдельная афиша и карьерная витрина; лучше вести вручную или полуавтоматически.",
        mode="manual_only",
    ),
    "ai_humor_lab": TopicDefinition(
        slug="ai_humor_lab",
        title="ИИ Мемы и наблюдения",
        description="Мемы, легкие наблюдения, реакционные посты и культурный контекст вокруг AI.",
        channel_idea="Легкий побочный канал, если нужна более широкая вовлеченность без смешивания с основным техническим потоком.",
        mode="optional",
    ),
}


@app.callback()
def main() -> None:
    """Topic and channel audit commands."""


@dataclass
class LinkStats:
    total: int = 0
    article_like_total: int = 0
    article_like_summaries: int = 0
    pending_article_summaries: int = 0
    failed_article_summaries: int = 0
    loaded_total: int = 0
    domains: Counter[str] = field(default_factory=Counter)


@dataclass
class MediaStats:
    total: int = 0
    done: int = 0
    image_done: int = 0


@dataclass
class AuditEntry:
    entry_id: int
    post_id: int
    content_item_id: int | None
    draft_id: int | None
    chat_title: str
    chat_username: str | None
    post_date: str | None
    received_at: str | None
    last_operation_at: str | None
    stage: str
    status: str
    title: str
    preview: str
    genre_primary: str | None
    genre_secondary: list[str]
    confidence: float | None
    difficulty_score: int | None
    promo_score: int | None
    opinion_score: int | None
    event_score: int | None
    is_eligible: bool
    eligibility_reason: str | None
    publication_allowed: bool
    blocked_reason: str | None
    has_content_item: bool
    has_draft: bool
    draft_ready: bool
    has_media: bool
    has_image: bool
    links_total: int
    article_links_total: int
    article_summaries: int
    pending_article_summaries: int
    failed_article_summaries: int
    topics: list[str]
    publishable: bool
    source_url: str

    def sample_row(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "post_id": self.post_id,
            "content_item_id": self.content_item_id,
            "draft_id": self.draft_id,
            "chat": self.chat_title,
            "chat_username": self.chat_username,
            "post_date": self.post_date,
            "stage": self.stage,
            "status": self.status,
            "title": self.title,
            "preview": self.preview,
            "genre_primary": self.genre_primary,
            "genre_secondary": self.genre_secondary,
            "difficulty_score": self.difficulty_score,
            "promo_score": self.promo_score,
            "opinion_score": self.opinion_score,
            "event_score": self.event_score,
            "is_eligible": self.is_eligible,
            "eligibility_reason": self.eligibility_reason,
            "has_media": self.has_media,
            "has_image": self.has_image,
            "links_total": self.links_total,
            "article_links_total": self.article_links_total,
            "article_summaries": self.article_summaries,
            "pending_article_summaries": self.pending_article_summaries,
            "failed_article_summaries": self.failed_article_summaries,
            "topics": self.topics,
            "publishable": self.publishable,
            "source_url": self.source_url,
        }


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def short_text(value: Any, limit: int = 360) -> str:
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def iso_dt(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def telegram_post_source_url(post: TelegramPost, chat: TelegramChat | None) -> str:
    if chat and chat.username:
        return f"https://t.me/{chat.username}/{post.message_id}"
    chat_id = str(post.chat_peer_id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{post.message_id}"
    return f"telegram:{post.chat_peer_id}/{post.message_id}"


def contains_any_keyword(text: str, keywords: Iterable[str]) -> bool:
    normalized = text.lower()
    return any(keyword in normalized for keyword in keywords)


def topic_slugs_for_entry(entry: Mapping[str, Any]) -> list[str]:
    genre = clean_text(entry.get("genre_primary")).lower()
    promo_score = to_int(entry.get("promo_score"), 0)
    event_score = to_int(entry.get("event_score"), 0)
    difficulty_score = to_int(entry.get("difficulty_score"), 0)
    text = " ".join(
        [
            clean_text(entry.get("title")),
            clean_text(entry.get("preview")),
            clean_text(entry.get("genre_secondary")),
        ]
    )
    if genre in NOISE_GENRES:
        return []
    topics: list[str] = []
    if genre == "technical_research" and difficulty_score >= 3 and promo_score <= 1:
        topics.append("ai_research_engineering")
    if genre == "tool_product" and promo_score <= 1:
        topics.append("ai_tools_products")
    if genre == "business_market" and promo_score <= 1:
        topics.append("ai_business_strategy")
    if genre == "opinion_commentary" and promo_score <= 1 and contains_any_keyword(text, BUSINESS_KEYWORDS):
        topics.append("ai_business_strategy")
    if genre == "news_announcement" and promo_score <= 1 and event_score <= 2:
        topics.append("ai_news_monitor")
    if genre in {"event_webinar", "career_job"} and promo_score <= 2:
        topics.append("ai_events_jobs")
    if genre == "humor_meme":
        topics.append("ai_humor_lab")
    return topics


def entry_has_publishable_basics(entry: Mapping[str, Any]) -> bool:
    if not bool(entry.get("is_eligible")):
        return False
    if not bool(entry.get("publication_allowed", True)):
        return False
    if clean_text(entry.get("genre_primary")).lower() in NOISE_GENRES:
        return False
    if to_int(entry.get("promo_score"), 0) > 1:
        return False
    if not bool(entry.get("has_image")):
        return False
    article_total = to_int(entry.get("article_links_total"), 0)
    article_summaries = to_int(entry.get("article_summaries"), 0)
    if article_total and article_summaries < article_total:
        return False
    return not clean_text(entry.get("status")).lower() in {
        "blocked",
        "missing_media",
        "link_summary_failed",
        "publish_failed",
        "publish_failed_media",
        "publish_failed_media_verification",
    }


def token_counter(texts: Iterable[str], limit: int = 20) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for text in texts:
        for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9_+-]{2,}", text.lower()):
            if token in STOP_WORDS or token.isdigit():
                continue
            counter[token] += 1
    return [{"term": term, "count": count} for term, count in counter.most_common(limit)]


def recommendation_for_topic(slug: str, count: int, publishable_count: int) -> str:
    if slug in OPTIONAL_MANUAL_TOPICS:
        return "manual_only" if count >= 20 else "collect_more"
    if publishable_count >= 100 or count >= 180:
        return "open_now"
    if publishable_count >= 40 or count >= 80:
        return "collect_more"
    return "do_not_open_yet"


def training_target_count(count: int, publishable_count: int) -> int:
    if count <= 0:
        return 0
    base = max(40, min(200, count // 3))
    if publishable_count >= 100:
        base = max(base, 100)
    return min(count, base)


def summarize_entries(entries: list[AuditEntry], *, generated_at: datetime, scope: str, sample_per_topic: int) -> dict[str, Any]:
    breakdowns = {
        "stage": Counter(entry.stage or "unknown" for entry in entries),
        "status": Counter(entry.status or "unknown" for entry in entries),
        "genre": Counter(entry.genre_primary or "unknown" for entry in entries),
        "eligibility_reason": Counter(entry.eligibility_reason or "unknown" for entry in entries),
    }
    totals = {
        "entries": len(entries),
        "unpublished": len(entries),
        "with_content_item": sum(1 for entry in entries if entry.has_content_item),
        "with_draft": sum(1 for entry in entries if entry.has_draft),
        "draft_ready": sum(1 for entry in entries if entry.draft_ready),
        "eligible": sum(1 for entry in entries if entry.is_eligible),
        "publication_allowed": sum(1 for entry in entries if entry.publication_allowed),
        "with_media": sum(1 for entry in entries if entry.has_media),
        "with_image": sum(1 for entry in entries if entry.has_image),
        "publishable_basics": sum(1 for entry in entries if entry.publishable),
        "article_links": sum(entry.article_links_total for entry in entries),
        "article_summaries": sum(entry.article_summaries for entry in entries),
        "pending_article_summaries": sum(entry.pending_article_summaries for entry in entries),
        "failed_article_summaries": sum(entry.failed_article_summaries for entry in entries),
    }
    topic_entries: dict[str, list[AuditEntry]] = defaultdict(list)
    for entry in entries:
        for slug in entry.topics:
            topic_entries[slug].append(entry)

    topics: list[dict[str, Any]] = []
    for slug, definition in TOPIC_DEFINITIONS.items():
        rows = topic_entries.get(slug, [])
        publishable_count = sum(1 for entry in rows if entry.publishable)
        avg = lambda attr: (sum(to_int(getattr(entry, attr), 0) for entry in rows) / len(rows)) if rows else 0.0
        chat_counter = Counter(entry.chat_title or "unknown" for entry in rows)
        topic_domains: Counter[str] = Counter()
        for entry in rows:
            # Domain details are flattened in sample data only; topic-level terms still give a useful signal.
            if entry.source_url.startswith("https://t.me/"):
                topic_domains["t.me"] += 1
        top_terms = token_counter((f"{entry.title} {entry.preview}" for entry in rows), limit=16)
        topics.append(
            {
                "slug": slug,
                "title": definition.title,
                "description": definition.description,
                "channel_idea": definition.channel_idea,
                "mode": definition.mode,
                "count": len(rows),
                "publishable_count": publishable_count,
                "eligible_count": sum(1 for entry in rows if entry.is_eligible),
                "with_image_count": sum(1 for entry in rows if entry.has_image),
                "with_draft_count": sum(1 for entry in rows if entry.has_draft),
                "article_links_count": sum(entry.article_links_total for entry in rows),
                "article_summaries_count": sum(entry.article_summaries for entry in rows),
                "avg_difficulty": round(avg("difficulty_score"), 2),
                "avg_promo": round(avg("promo_score"), 2),
                "avg_opinion": round(avg("opinion_score"), 2),
                "avg_event": round(avg("event_score"), 2),
                "recommendation": recommendation_for_topic(slug, len(rows), publishable_count),
                "training_target": training_target_count(len(rows), publishable_count),
                "top_chats": [{"chat": chat, "count": count} for chat, count in chat_counter.most_common(10)],
                "top_domains": [{"domain": domain, "count": count} for domain, count in topic_domains.most_common(10)],
                "top_terms": top_terms,
                "samples": [entry.sample_row() for entry in rows[:sample_per_topic]],
            }
        )

    noise_entries = [entry for entry in entries if clean_text(entry.genre_primary).lower() in NOISE_GENRES]
    training = {
        "taxonomy_source": "config/yandexgpt_genre_axes.yaml + topic audit rules",
        "positive_topics": [
            {
                "topic": topic["slug"],
                "title": topic["title"],
                "available": topic["count"],
                "publishable": topic["publishable_count"],
                "target_labels": topic["training_target"],
                "priority": topic["recommendation"],
            }
            for topic in topics
            if topic["count"] > 0
        ],
        "negative_noise": [
            {
                "genre": genre,
                "available": count,
                "target_labels": min(60, count),
                "purpose": "hard negative: не публиковать и не открывать отдельный канал",
            }
            for genre, count in Counter(entry.genre_primary or "unknown" for entry in noise_entries).most_common()
        ],
        "recommended_holdout_percent": 20,
        "notes": [
            "Сначала разметить positive topics и hard negatives, затем обучить route/topic classifier поверх существующей жанровой модели.",
            "Для автопубликации каждого нового канала отдельно проверять media_required, link summaries и promo_score.",
        ],
    }

    return {
        "generated_at": generated_at.isoformat(),
        "scope": scope,
        "totals": totals,
        "breakdowns": {key: dict(counter.most_common()) for key, counter in breakdowns.items()},
        "topics": topics,
        "training": training,
    }


def write_summary_markdown(summary: Mapping[str, Any]) -> str:
    totals = summary.get("totals", {})
    lines = [
        "# Аудит неопубликованных AI-постов",
        "",
        f"Сгенерировано: `{summary.get('generated_at')}`",
        f"Scope: `{summary.get('scope')}`",
        "",
        "## Общая статистика",
        "",
        f"- Неопубликованных карточек: **{totals.get('unpublished', 0)}**",
        f"- С материалом content_item: **{totals.get('with_content_item', 0)}**",
        f"- С черновиком: **{totals.get('with_draft', 0)}**",
        f"- Подходят по текущим правилам: **{totals.get('eligible', 0)}**",
        f"- С изображением: **{totals.get('with_image', 0)}**",
        f"- Потенциально публикуемые после проверок: **{totals.get('publishable_basics', 0)}**",
        f"- Article-like ссылок: **{totals.get('article_links', 0)}**, summaries: **{totals.get('article_summaries', 0)}**",
        "",
        "## Кандидаты каналов",
        "",
        "| Тема | Всего | Потенциально публикуемые | Рекомендация | Что это |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for topic in summary.get("topics", []):
        lines.append(
            f"| {topic['title']} (`{topic['slug']}`) | {topic['count']} | {topic['publishable_count']} | "
            f"{topic['recommendation']} | {topic['description']} |"
        )
    lines.extend(["", "## Жанры", ""])
    for genre, count in (summary.get("breakdowns", {}).get("genre") or {}).items():
        lines.append(f"- `{genre}`: {count}")
    return "\n".join(lines).strip() + "\n"


def write_training_plan_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        "# План дообучения topic/router модели",
        "",
        "Цель: поверх существующей жанровой модели научить отдельный route/topic classifier выбирать новый MAX-чат или оставлять пост в основном канале/отбраковке.",
        "",
        "## Разметка",
        "",
        "| Тема | Доступно | Публикуемых | Цель разметки | Приоритет |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for item in summary.get("training", {}).get("positive_topics", []):
        lines.append(
            f"| {item['title']} (`{item['topic']}`) | {item['available']} | {item['publishable']} | "
            f"{item['target_labels']} | {item['priority']} |"
        )
    lines.extend(["", "## Hard negatives", ""])
    for item in summary.get("training", {}).get("negative_noise", []):
        lines.append(f"- `{item['genre']}`: взять до {item['target_labels']} примеров как негативный класс.")
    lines.extend(
        [
            "",
            "## Проверка качества",
            "",
            "- Holdout: 20% по каждому topic-классу.",
            "- Минимум перед автозапуском: macro F1 >= 0.75, precision для publishable topic >= 0.85.",
            "- Отдельно считать ошибки между `technical_research`, `tool_product`, `business_market` и `news_announcement`.",
            "- После обучения прогнать dry-run по всем неопубликованным постам и сравнить распределение с этим аудитом.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def report_root(settings: Settings) -> Path:
    return Path(settings.reports_dir) / "topic_audit"


def latest_topic_audit_summary(root: Path) -> dict[str, Any] | None:
    if not root.exists():
        return None
    candidates = sorted(root.glob("*/summary.json"), key=lambda path: (path.stat().st_mtime, path.parent.name), reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data.setdefault("artifacts", {})
        data["artifacts"].setdefault("summary_json", str(path))
        return data
    return None


def write_report_files(summary: dict[str, Any], entries: list[AuditEntry], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    summary_md_path = output_dir / "summary.md"
    topics_path = output_dir / "topics.csv"
    samples_path = output_dir / "samples.jsonl"
    training_plan_path = output_dir / "training_plan.md"

    summary["artifacts"] = {
        "summary_json": str(summary_path),
        "summary_md": str(summary_md_path),
        "topics_csv": str(topics_path),
        "samples_jsonl": str(samples_path),
        "training_plan_md": str(training_plan_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md_path.write_text(write_summary_markdown(summary), encoding="utf-8")
    training_plan_path.write_text(write_training_plan_markdown(summary), encoding="utf-8")

    topic_fields = [
        "slug",
        "title",
        "mode",
        "count",
        "publishable_count",
        "eligible_count",
        "with_image_count",
        "with_draft_count",
        "article_links_count",
        "article_summaries_count",
        "avg_difficulty",
        "avg_promo",
        "avg_opinion",
        "avg_event",
        "recommendation",
        "training_target",
    ]
    with topics_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=topic_fields)
        writer.writeheader()
        for topic in summary.get("topics", []):
            writer.writerow({field_name: topic.get(field_name) for field_name in topic_fields})

    with samples_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            if not entry.topics:
                continue
            handle.write(json.dumps(entry.sample_row(), ensure_ascii=False) + "\n")
    return summary["artifacts"]


async def load_audit_entries(settings: Settings, *, limit: int | None = None) -> list[AuditEntry]:
    async with session_factory(settings)() as session:
        stmt = (
            select(PipelineEntry, TelegramPost, TelegramChat, ContentItem, PublicationDraft)
            .select_from(PipelineEntry)
            .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
            .join(TelegramChat, TelegramChat.peer_id == TelegramPost.chat_peer_id)
            .outerjoin(ContentItem, ContentItem.id == PipelineEntry.content_item_id)
            .outerjoin(PublicationDraft, PublicationDraft.id == PipelineEntry.latest_draft_id)
            .where(
                TelegramChat.folder_name == settings.folder_name,
                TelegramPost.is_deleted.is_(False),
                PipelineEntry.published_post_id.is_(None),
            )
            .order_by(PipelineEntry.last_operation_at.desc(), PipelineEntry.id.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).all())
        post_ids = [row[0].source_post_id for row in rows]
        link_stats: dict[int, LinkStats] = defaultdict(LinkStats)
        media_stats: dict[int, MediaStats] = defaultdict(MediaStats)
        if post_ids:
            link_rows = await session.execute(
                select(
                    PostLink.post_id,
                    PostLink.url_type,
                    PostLink.extraction_status,
                    PostLink.domain,
                    LinkSnapshot.summary_short,
                    LinkSnapshot.error,
                )
                .outerjoin(LinkSnapshot, LinkSnapshot.link_id == PostLink.id)
                .where(PostLink.post_id.in_(post_ids))
            )
            for post_id, url_type, extraction_status, domain, summary_short, error in link_rows.all():
                stats = link_stats[int(post_id)]
                stats.total += 1
                if domain:
                    stats.domains[domain] += 1
                if str(extraction_status or "").lower() == "done":
                    stats.loaded_total += 1
                if str(url_type or "").lower() in ARTICLE_LIKE_URL_TYPES:
                    stats.article_like_total += 1
                    if clean_text(summary_short):
                        stats.article_like_summaries += 1
                    elif str(extraction_status or "").lower() == "failed" or clean_text(error):
                        stats.failed_article_summaries += 1
                    else:
                        stats.pending_article_summaries += 1

            media_rows = await session.execute(
                select(MediaAsset.source_post_id, MediaAsset.download_status, MediaAsset.mime_type)
                .where(MediaAsset.source_post_id.in_(post_ids))
            )
            for post_id, download_status, mime_type in media_rows.all():
                if post_id is None:
                    continue
                stats = media_stats[int(post_id)]
                stats.total += 1
                done = str(download_status or "").lower() == "done"
                if done:
                    stats.done += 1
                if done and str(mime_type or "").lower().startswith("image/"):
                    stats.image_done += 1

        entries: list[AuditEntry] = []
        for entry, post, chat, item, draft in rows:
            links = link_stats[entry.source_post_id]
            media = media_stats[entry.source_post_id]
            first_post_line = (post.text or "").splitlines()[0] if post.text and (post.text or "").splitlines() else None
            title = clean_text(
                (draft.title if draft else None)
                or (item.translated_title if item else None)
                or (item.title if item else None)
                or first_post_line
            )
            preview = short_text(
                (draft.body if draft else None)
                or (item.translated_summary if item else None)
                or (item.source_summary if item else None)
                or (item.main_text if item else None)
                or post.text
            )
            base: dict[str, Any] = {
                "title": title,
                "preview": preview,
                "genre_primary": entry.genre_primary,
                "genre_secondary": list(entry.genre_secondary or []),
                "difficulty_score": entry.difficulty_score,
                "promo_score": entry.promo_score,
                "opinion_score": entry.opinion_score,
                "event_score": entry.event_score,
                "is_eligible": bool(entry.is_eligible),
                "publication_allowed": bool(entry.publication_allowed),
                "status": entry.status,
                "has_image": bool((draft and draft.image_asset_id) or (item and item.primary_image_asset_id) or media.image_done),
                "article_links_total": links.article_like_total,
                "article_summaries": links.article_like_summaries,
            }
            topics = topic_slugs_for_entry(base)
            publishable = entry_has_publishable_basics(base)
            entries.append(
                AuditEntry(
                    entry_id=entry.id,
                    post_id=entry.source_post_id,
                    content_item_id=entry.content_item_id,
                    draft_id=entry.latest_draft_id,
                    chat_title=chat.title or str(chat.peer_id),
                    chat_username=chat.username,
                    post_date=iso_dt(post.date),
                    received_at=iso_dt(post.created_at),
                    last_operation_at=iso_dt(entry.last_operation_at),
                    stage=entry.stage,
                    status=entry.status,
                    title=title or "Без заголовка",
                    preview=preview,
                    genre_primary=entry.genre_primary,
                    genre_secondary=list(entry.genre_secondary or []),
                    confidence=to_float(entry.confidence),
                    difficulty_score=entry.difficulty_score,
                    promo_score=entry.promo_score,
                    opinion_score=entry.opinion_score,
                    event_score=entry.event_score,
                    is_eligible=bool(entry.is_eligible),
                    eligibility_reason=entry.eligibility_reason,
                    publication_allowed=bool(entry.publication_allowed),
                    blocked_reason=entry.blocked_reason,
                    has_content_item=item is not None,
                    has_draft=draft is not None,
                    draft_ready=bool(draft and draft.status == READY_DRAFT_STATUS),
                    has_media=bool(media.done or (draft and draft.image_asset_id) or (item and item.primary_image_asset_id)),
                    has_image=base["has_image"],
                    links_total=links.total,
                    article_links_total=links.article_like_total,
                    article_summaries=links.article_like_summaries,
                    pending_article_summaries=links.pending_article_summaries,
                    failed_article_summaries=links.failed_article_summaries,
                    topics=topics,
                    publishable=publishable,
                    source_url=telegram_post_source_url(post, chat),
                )
            )
        return entries


async def build_topic_audit(
    settings: Settings,
    *,
    scope: str = "all-unpublished",
    sample_per_topic: int = 30,
    limit: int | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    if scope != "all-unpublished":
        raise ValueError("Only scope='all-unpublished' is supported.")
    generated_at = datetime.now(timezone.utc)
    entries = await load_audit_entries(settings, limit=limit)
    summary = summarize_entries(entries, generated_at=generated_at, scope=scope, sample_per_topic=sample_per_topic)
    run_dir = output_dir or report_root(settings) / generated_at.strftime("%Y%m%d_%H%M%S")
    write_report_files(summary, entries, run_dir)
    return summary


@app.command("audit-unpublished")
def audit_unpublished_command(
    scope: str = typer.Option("all-unpublished", "--scope", help="Audit scope. Currently only all-unpublished."),
    sample_per_topic: int = typer.Option(30, "--sample-per-topic", min=1, max=200),
    limit: int = typer.Option(0, "--limit", min=0, help="Optional DB row limit for smoke tests. 0 means all rows."),
    output_dir: Path | None = typer.Option(None, "--output-dir", file_okay=False, dir_okay=True),
) -> None:
    """Build topic/channel candidates from unpublished MAX pipeline entries."""

    settings = settings_or_exit()
    summary = run_async(
        build_topic_audit(
            settings,
            scope=scope,
            sample_per_topic=sample_per_topic,
            limit=limit or None,
            output_dir=output_dir,
        )
    )
    artifacts = summary.get("artifacts", {})
    typer.echo(
        json.dumps(
            {
                "unpublished": summary.get("totals", {}).get("unpublished", 0),
                "publishable_basics": summary.get("totals", {}).get("publishable_basics", 0),
                "summary": artifacts.get("summary_md"),
                "topics": artifacts.get("topics_csv"),
                "samples": artifacts.get("samples_jsonl"),
                "training_plan": artifacts.get("training_plan_md"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    app()
