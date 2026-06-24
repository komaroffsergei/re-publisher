from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
import re
from typing import Any

import typer
import yaml
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.local_llm import complete as complete_local_llm, local_model_label
from app.content.state import mark_state
from app.content.text_utils import clean_text
from app.content.yandex_gpt import (
    YandexGPTError,
    build_rewrite_messages,
    complete,
    label_prompts,
    load_prompt_config,
    model_name_from_uri,
    model_uri,
    parse_rewrite_response,
    sanitize_editorial_text,
    strip_json_fence,
)
from app.main import safe_echo
from app.models import ContentItem, PostClassification, PublicationDraft, PublicationTarget, Showcase

app = typer.Typer(no_args_is_help=True)
LINK_PRESERVATION_INSTRUCTION = (
    "Исключение из запрета markdown: если Telegram-пост содержит inline-ссылки вида [текст](url), "
    "сохрани эти ссылки в body с теми же URL и естественными anchor-текстами. Не выноси ссылки в конец, "
    "если они в исходнике являются частью конкретной строки или фразы."
)


@app.callback()
def main() -> None:
    """Rewrite commands."""


def load_templates(path: Path = Path("config/rewrite_templates.yaml")) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("templates") or {}


def clip_text(text: str, max_chars: int) -> str:
    text = clean_text(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].strip()


def strip_visual_noise(text: str) -> str:
    text = re.sub(
        "["
        "\U0001f300-\U0001f5ff"
        "\U0001f600-\U0001f64f"
        "\U0001f680-\U0001f6ff"
        "\U0001f900-\U0001f9ff"
        "\u2600-\u27bf"
        "]+",
        "",
        text,
    )
    return clean_text(text)


def quoted_topic(text: str) -> str | None:
    match = re.search(r"[\"«](.{12,180}?)[\"»]", text)
    return clean_text(match.group(1)) if match else None


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", strip_visual_noise(text))
    return [clean_text(part) for part in parts if len(clean_text(part)) >= 12]


def editorial_post_rewrite(item: ContentItem, label: str | None = None) -> str:
    source_text = strip_visual_noise(item.main_text or "")
    title = strip_visual_noise(item.translated_title or item.title or "")
    lower = source_text.lower()
    parts = sentences(source_text)
    topic = quoted_topic(source_text)

    if "мем" in lower or label == "humor_meme":
        setup = topic or next((part for part in parts if "xgboost" in part.lower() or "llm" in part.lower()), title)
        return clean_text(
            "Автор возвращается после короткой паузы и делится мемом про то, как вокруг генеративного ИИ иногда возникает лишняя путаница. "
            f"Смысл шутки простой: не каждую задачу нужно сводить к большим языковым моделям. Повод для обсуждения: {setup}."
        )

    if "вебинар" in lower or "запись" in lower:
        topic_text = topic or title
        return clean_text(
            f"В посте анонсирована или опубликована запись AI-вебинара. Тема выпуска: {topic_text}. "
            "Материал можно использовать как заметку о событии после проверки ссылки, спикера и даты."
        )

    if "ваканси" in lower or "ищем" in lower:
        detail = parts[0] if parts else title
        return clean_text(
            f"Пост выглядит как карьерное или промо-объявление. Главный повод: {detail}. "
            "Перед публикацией стоит проверить условия, контакты и актуальность предложения."
        )

    if "релиз" in lower or "выпуст" in lower or "запуст" in lower:
        detail = parts[0] if parts else title
        return clean_text(
            f"В посте говорится о новом запуске или обновлении в AI-сфере. Основная мысль: {detail}. "
            "Факты лучше сверить с первоисточником перед публикацией."
        )

    if label == "news_digest":
        detail = parts[0] if parts else title
        return clean_text(f"Появилось короткое обновление по теме AI: {detail}. Перед публикацией стоит уточнить контекст и источник.")

    if label == "opinion_commentary":
        detail = parts[0] if parts else title
        return clean_text(f"Авторский пост с наблюдением о рынке или практике AI. Основной тезис: {detail}.")

    if parts:
        detail = parts[0]
        return clean_text(f"Пост можно использовать как короткую редакционную заметку. Главный повод: {detail}.")
    return title or "Материал требует ручной редакторской обработки."


def local_fallback_summary(item: ContentItem, label: str | None = None) -> str:
    source_text = strip_visual_noise(item.main_text or "")
    title = strip_visual_noise(item.translated_title or item.title or "")
    lower = source_text.lower()
    topic = quoted_topic(source_text) or title
    if "вебинар" in lower or "запись" in lower and "youtube" in (item.source_domain or ""):
        return clean_text(
            f"Опубликована запись AI-вебинара. Тема: {topic}. "
            "Материал относится к событиям и обучающему контенту; перед публикацией стоит проверить ссылку, спикера и актуальность формулировок."
        )
    if "ваканси" in lower or "ищем" in lower:
        return clean_text(
            f"Пост похож на карьерное объявление или промо. Основной повод: {topic}. "
            "Такой материал лучше отправлять в витрину вакансий, событий или промо после ручной проверки условий."
        )
    if "релиз" in lower or "выпуст" in lower or "запуст" in lower:
        return clean_text(
            f"Сообщается о новом AI-релизе или запуске. Главная тема: {topic}. "
            "Перед публикацией нужно сверить источник и отделить подтвержденные факты от авторского комментария."
        )
    if "github" in lower or "arxiv" in lower or "модель" in lower:
        return clean_text(
            f"Материал касается технической темы в AI. Рабочая формулировка: {topic}. "
            "Черновик требует проверки источника, деталей реализации и ограничений."
        )
    return editorial_post_rewrite(item, label)


def draft_from_template(item: ContentItem, showcase: Showcase, template: dict[str, Any], label: str | None = None) -> dict[str, Any]:
    source = item.source_url or item.source_domain
    has_source_summary = bool(clean_text(item.translated_summary or item.source_summary))
    if has_source_summary:
        summary = clean_text(item.translated_summary or item.source_summary)
    elif item.source_url:
        summary = local_fallback_summary(item, label)
    else:
        summary = editorial_post_rewrite(item, label)
    title_base = clean_text(item.translated_title or item.title) or "Материал"
    prefix = clean_text(template.get("title_prefix"))
    title = f"{prefix} {title_base}".strip() if prefix else title_base
    max_body_chars = int(template.get("max_body_chars") or 1800)
    if source:
        body_format = template.get("body_format") or "{summary}\n\nИсточник: {source}"
        body = body_format.format(summary=summary, source=source)
    else:
        body = summary
    body = clip_text(body, max_body_chars)
    risk_flags: list[str] = []
    if not item.source_url:
        risk_flags.append("missing_source_url")
    if item.source_url and not has_source_summary:
        risk_flags.append("missing_source_summary")
    if len(summary) < 80:
        risk_flags.append("thin_source")
    similarity = SequenceMatcher(None, clean_text(item.main_text), body).ratio() if item.main_text and body else 0.0
    return {
        "title": title[:240],
        "body": body,
        "source_url": item.source_url,
        "source_domain": item.source_domain,
        "image_asset_id": item.primary_image_asset_id,
        "risk_flags": risk_flags,
        "similarity_to_original": round(similarity, 4),
    }


def base_risk_flags(item: ContentItem, source_summary: str) -> list[str]:
    risk_flags: list[str] = []
    if not item.source_url:
        risk_flags.append("missing_source_url")
    if item.source_url and not source_summary:
        risk_flags.append("missing_source_summary")
    if len(source_summary or clean_text(item.main_text)) < 80:
        risk_flags.append("thin_source")
    return risk_flags


def remove_label_names(text: str, labels: set[str]) -> str:
    cleaned = text
    for label in labels:
        cleaned = re.sub(rf"\b{re.escape(label)}\b", "", cleaned, flags=re.IGNORECASE)
    return clean_text(cleaned)


def empty_content_draft(item: ContentItem) -> dict[str, Any]:
    body = (
        "В исходной публикации нет текстового контекста: вероятно, это медиа или пустой пост. "
        "Перед публикацией нужен ручной просмотр вложения и проверка, можно ли использовать материал как самостоятельную заметку."
    )
    return {
        "title": clean_text(item.translated_title or item.title) or "Материал требует просмотра",
        "body": body,
        "risk_flags": ["media_only_or_empty"],
        "claims": [],
    }


def prompt_config_with_link_preservation(prompt_config: dict[str, Any], post_text: str | None) -> dict[str, Any]:
    if "](" not in (post_text or ""):
        return prompt_config
    rewrite_config = dict(prompt_config.get("rewrite") or {})
    rewrite_config["system"] = clean_text(f"{rewrite_config.get('system') or ''}\n\n{LINK_PRESERVATION_INSTRUCTION}")
    return {**prompt_config, "rewrite": rewrite_config}


async def draft_from_yandex(
    item: ContentItem,
    showcase: Showcase,
    template: dict[str, Any],
    label: str | None,
    settings,
    prompt_config: dict[str, Any],
    source_summary_override: str | None = None,
    post_text_override: str | None = None,
) -> dict[str, Any]:
    source_summary = clean_text(source_summary_override or item.translated_summary or item.source_summary)
    post_text = post_text_override if post_text_override is not None else item.main_text
    prompt_config = prompt_config_with_link_preservation(prompt_config, post_text)
    title_base = clean_text(item.translated_title or item.title) or "Материал"
    model = model_uri(settings, "rewrite")
    system_prompt, user_prompt = build_rewrite_messages(
        prompt_config,
        label=label,
        title=title_base,
        post_text=post_text,
        source_summary=source_summary,
        source_url=item.source_url,
        showcase_title=showcase.title,
        max_input_chars=max(1800, settings.summary_max_input_tokens * 8),
    )
    completion = await complete(
        settings,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.35,
        max_tokens=max(700, int(template.get("max_body_chars") or 1800) // 2),
    )
    try:
        parsed = parse_rewrite_response(completion.text)
    except YandexGPTError as exc:
        salvaged_body = sanitize_editorial_text(strip_json_fence(completion.text))
        if salvaged_body and "{" not in salvaged_body[:20]:
            parsed = {"title": title_base, "body": salvaged_body, "risk_flags": ["json_salvaged"], "claims": []}
        elif "body is empty" not in str(exc) or clean_text(item.main_text) or source_summary:
            raise
        else:
            parsed = empty_content_draft(item)
    known_labels = set(label_prompts(prompt_config))
    max_body_chars = int(template.get("max_body_chars") or 1800)
    body = clip_text(remove_label_names(sanitize_editorial_text(parsed["body"]), known_labels), max_body_chars)
    title = remove_label_names(sanitize_editorial_text(parsed["title"]), known_labels)[:240] or title_base[:240]
    risk_flags = [*base_risk_flags(item, source_summary)]
    for flag in parsed["risk_flags"]:
        if flag not in risk_flags:
            risk_flags.append(flag)
    similarity = SequenceMatcher(None, clean_text(item.main_text), body).ratio() if item.main_text and body else 0.0
    return {
        "title": title,
        "body": body,
        "source_url": item.source_url,
        "source_domain": item.source_domain,
        "image_asset_id": item.primary_image_asset_id,
        "claims": parsed["claims"],
        "risk_flags": risk_flags,
        "similarity_to_original": round(similarity, 4),
        "rewrite_model": model_name_from_uri(completion.model_uri),
    }


async def draft_from_local_llm(
    item: ContentItem,
    showcase: Showcase,
    template: dict[str, Any],
    label: str | None,
    settings,
    prompt_config: dict[str, Any],
    source_summary_override: str | None = None,
    post_text_override: str | None = None,
) -> dict[str, Any]:
    source_summary = clean_text(source_summary_override or item.translated_summary or item.source_summary)
    post_text = post_text_override if post_text_override is not None else item.main_text
    title_base = clean_text(item.translated_title or item.title) or "Материал"
    known_labels = set(label_prompts(prompt_config))
    labels = label_prompts(prompt_config)
    label_key = label or "news_digest"
    label_instruction = clean_text(labels.get(label_key) or labels.get("news_digest") or "Сделай аккуратный рерайт.")
    system_prompt = clean_text(
        """
        Ты редактор русскоязычного Telegram-канала об искусственном интеллекте.
        Сделай рерайт без авторства, без слова "Коротко", без служебных пояснений и без markdown, кроме исходных inline-ссылок [текст](url).
        Сохрани исходные inline-ссылки в body с теми же URL и естественными anchor-текстами.
        Не добавляй факты вне исходного поста и summary ссылки.
        Верни только валидный компактный JSON:
        {"title":"...","body":"...","risk_flags":[],"claims":[]}
        После закрывающей скобки ничего не пиши.
        """
    )
    user_prompt = clean_text(
        f"""
        Жанр: {label_key}
        Задача жанра: {label_instruction}
        Заголовок: {title_base}
        Telegram-пост: {clip_text(post_text or "", 1800)}
        Summary ссылки: {clip_text(source_summary, 900) or "нет"}
        Источник: {clean_text(item.source_url) or "нет"}
        Витрина: {clean_text(showcase.title)}

        Требования к JSON:
        - title: новый редакционный заголовок до 90 символов;
        - body: 2-3 коротких абзаца, максимум 900 символов;
        - quotes не рерайтить, если они есть;
        - если есть английский фрагмент, под ним добавь русский перевод курсивной разметкой;
        - risk_flags: [] если явных рисков нет;
        - claims: [].
        """
    )
    completion = await complete_local_llm(
        settings,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=settings.local_llm_temperature,
        max_tokens=settings.local_llm_max_tokens,
        json_object=True,
    )
    try:
        parsed = parse_rewrite_response(completion.text)
    except YandexGPTError as exc:
        if "body is empty" not in str(exc) or clean_text(item.main_text) or source_summary:
            raise
        parsed = empty_content_draft(item)
    max_body_chars = int(template.get("max_body_chars") or 1800)
    body = clip_text(remove_label_names(sanitize_editorial_text(parsed["body"]), known_labels), max_body_chars)
    title = remove_label_names(sanitize_editorial_text(parsed["title"]), known_labels)[:240] or title_base[:240]
    risk_flags = [*base_risk_flags(item, source_summary)]
    for flag in parsed["risk_flags"]:
        if flag not in risk_flags:
            risk_flags.append(flag)
    similarity = SequenceMatcher(None, clean_text(item.main_text), body).ratio() if item.main_text and body else 0.0
    return {
        "title": title,
        "body": body,
        "source_url": item.source_url,
        "source_domain": item.source_domain,
        "image_asset_id": item.primary_image_asset_id,
        "claims": parsed["claims"],
        "risk_flags": risk_flags,
        "similarity_to_original": round(similarity, 4),
        "rewrite_model": local_model_label(completion.model),
    }


async def rewrite_pending(limit: int, refresh: bool = False) -> int:
    settings = settings_or_exit()
    if not settings.enable_rewrite:
        return 0
    templates = load_templates()
    prompt_config = load_prompt_config() if settings.rewrite_backend in {"yandexgpt", "local_llm"} else {}
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        stmt = (
            select(PublicationTarget, ContentItem, Showcase, PostClassification, PublicationDraft)
            .join(ContentItem, ContentItem.id == PublicationTarget.content_item_id)
            .join(Showcase, Showcase.id == PublicationTarget.showcase_id)
            .outerjoin(PostClassification, PostClassification.content_item_id == ContentItem.id)
            .outerjoin(PublicationDraft, PublicationDraft.publication_target_id == PublicationTarget.id)
            .where(PublicationTarget.status == "pending")
            .order_by(PublicationTarget.id)
            .limit(limit)
        )
        if not refresh:
            stmt = stmt.where(PublicationDraft.id.is_(None))
        result = await session.execute(stmt)
        rows = list(result.all())
        table = PublicationDraft.__table__
        for target, item, showcase, classification, existing_draft in rows:
            template_name = showcase.default_rewrite_template or "news_short"
            label = classification.label_primary if classification else None
            template = templates.get(template_name) or {}
            try:
                if settings.rewrite_backend == "yandexgpt":
                    draft = await draft_from_yandex(item, showcase, template, label, settings, prompt_config)
                    rewrite_model = draft.get("rewrite_model") or "yandexgpt"
                elif settings.rewrite_backend == "local_llm":
                    draft = await draft_from_local_llm(item, showcase, template, label, settings, prompt_config)
                    rewrite_model = draft.get("rewrite_model") or "local_llm"
                elif settings.rewrite_backend == "local_template":
                    draft = draft_from_template(item, showcase, template, label)
                    draft["claims"] = []
                    rewrite_model = "local_template"
                else:
                    raise YandexGPTError(f"Unsupported REWRITE_BACKEND: {settings.rewrite_backend}")
            except YandexGPTError as exc:
                await mark_state(session, item.source_post_id, rewrite_status="failed", last_error=f"rewrite_yandexgpt: {str(exc)[:800]}")
                continue
            validation_errors = []
            status = "approved" if settings.auto_approve_drafts and not draft["risk_flags"] else "needs_review"
            values = {
                "publication_target_id": target.id,
                "source_post_id": item.source_post_id,
                "rewrite_model": rewrite_model,
                "rewrite_template": label or template_name,
                "title": draft["title"],
                "body": draft["body"] or "Текст требует ручной проверки.",
                "source_url": draft["source_url"],
                "source_domain": draft["source_domain"],
                "image_asset_id": draft["image_asset_id"],
                "tags": [classification.label_primary] if classification and classification.label_primary else [],
                "claims": draft.get("claims") or [],
                "risk_flags": draft["risk_flags"],
                "similarity_to_original": draft["similarity_to_original"],
                "validation_errors": validation_errors,
                "status": status,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            if existing_draft:
                await session.execute(
                    update(PublicationDraft)
                    .where(PublicationDraft.id == existing_draft.id)
                    .values(**{key: value for key, value in values.items() if key not in {"publication_target_id", "source_post_id", "created_at"}})
                )
            else:
                await session.execute(insert(table).values(**values))
            await mark_state(session, item.source_post_id, rewrite_status=status)
            count += 1
        await session.commit()
    return count


@app.command("rewrite-pending")
def rewrite_pending_command(
    limit: int = limit_option(),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh existing drafts as well as creating missing ones."),
) -> None:
    """Create publication drafts for pending publication targets."""

    count = run_async(rewrite_pending(limit, refresh=refresh))
    safe_echo(f"drafts={count}")


if __name__ == "__main__":
    app()
