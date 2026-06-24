from __future__ import annotations

import re
from datetime import datetime, timezone

import typer
from sqlalchemy import select, update

from app.config import Settings
from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.prompt_versions import LINK_SUMMARY_PROMPT, ensure_active_prompt_version, prompt_config_from_version
from app.content.state import mark_state
from app.content.text_utils import clean_text
from app.content.yandex_gpt import (
    YandexGPTError,
    build_summary_messages,
    complete,
    model_name_from_uri,
    model_uri,
)
from app.main import safe_echo
from app.models import LinkSnapshot, PostLink

app = typer.Typer(no_args_is_help=True)
_MODEL_CACHE: dict[str, tuple[object, object]] = {}


@app.callback()
def main() -> None:
    """Local summary commands."""


def split_paragraphs(text: str) -> list[str]:
    chunks = [clean_text(part) for part in re.split(r"\n{2,}|(?<=[.!?])\s+(?=[A-ZА-ЯЁ])", text)]
    return [chunk for chunk in chunks if len(chunk) >= 40]


def extractive_summary(text: str | None, title: str | None = None, max_chars: int = 900) -> str:
    cleaned = clean_text(text)
    if len(cleaned) <= max_chars:
        return cleaned
    paragraphs = split_paragraphs(cleaned)
    if not paragraphs:
        return cleaned[:max_chars].rsplit(" ", 1)[0].strip()
    query_terms = set(re.findall(r"[\wа-яА-ЯёЁ]{4,}", f"{title or ''} {cleaned[:500]}".lower()))
    scored: list[tuple[float, int, str]] = []
    for index, paragraph in enumerate(paragraphs):
        terms = re.findall(r"[\wа-яА-ЯёЁ]{4,}", paragraph.lower())
        overlap = sum(1 for term in terms if term in query_terms)
        score = overlap / max(1, len(set(terms))) + min(len(paragraph), 800) / 4000
        scored.append((score, index, paragraph))
    chosen = sorted(sorted(scored, reverse=True)[:3], key=lambda item: item[1])
    summary = clean_text("\n\n".join(item[2] for item in chosen))
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0].strip()
    return summary


def neural_summary(text: str, settings: Settings) -> str | None:
    backend = settings.summary_backend
    if backend not in {"rut5_absum", "rut5_gazeta"}:
        return None
    model_name = settings.summary_model_name if backend == "rut5_absum" else settings.summary_alt_model_name
    try:
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        if model_name not in _MODEL_CACHE:
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
            model.to(settings.summary_device)
            _MODEL_CACHE[model_name] = (tokenizer, model)
        tokenizer, model = _MODEL_CACHE[model_name]
        inputs = tokenizer(
            clean_text(text),
            max_length=settings.summary_max_input_tokens,
            truncation=True,
            return_tensors="pt",
        )
        output = model.generate(
            **inputs,
            max_length=settings.summary_max_output_tokens,
            num_beams=4,
            early_stopping=True,
        )
        return tokenizer.decode(output[0], skip_special_tokens=True).strip()
    except Exception:
        return None


def summarize_text(text: str | None, title: str | None, settings: Settings) -> tuple[str | None, str | None]:
    if not settings.enable_local_summary or not clean_text(text):
        return None, None
    neural = neural_summary(text or "", settings)
    if neural:
        return neural, settings.summary_backend
    return extractive_summary(text, title, max_chars=settings.summary_max_output_tokens * 6), "extractive_fallback"


async def yandex_summary(snapshot: LinkSnapshot, settings: Settings, prompt_config: dict) -> tuple[str | None, str | None]:
    if not clean_text(snapshot.extracted_text):
        return None, None
    model = model_uri(settings, "summary")
    system_prompt, user_prompt = build_summary_messages(
        prompt_config,
        title=snapshot.title,
        description=snapshot.description,
        extracted_text=snapshot.extracted_text,
        max_input_chars=max(1200, settings.summary_max_input_tokens * 6),
    )
    completion = await complete(
        settings,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.15,
        max_tokens=settings.summary_max_output_tokens,
    )
    return completion.text, model_name_from_uri(completion.model_uri)


async def summarize_pending(limit: int, refresh: bool = False) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        prompt_config = None
        if settings.summary_backend == "yandexgpt":
            prompt_version = await ensure_active_prompt_version(session, name=LINK_SUMMARY_PROMPT)
            prompt_config = prompt_config_from_version(prompt_version)
        stmt = (
            select(LinkSnapshot, PostLink.post_id)
            .join(PostLink, PostLink.id == LinkSnapshot.link_id)
            .where(LinkSnapshot.extracted_text.is_not(None))
            .order_by(LinkSnapshot.id)
            .limit(limit)
        )
        if not refresh:
            stmt = stmt.where(LinkSnapshot.summary_short.is_(None))
        result = await session.execute(stmt)
        rows = list(result.all())
        for snapshot, post_id in rows:
            try:
                if settings.summary_backend == "yandexgpt":
                    summary, model_name = await yandex_summary(snapshot, settings, prompt_config or {})
                else:
                    summary, model_name = summarize_text(snapshot.extracted_text, snapshot.title, settings)
            except YandexGPTError as exc:
                await mark_state(session, post_id, summary_status="failed", last_error=f"summary_yandexgpt: {str(exc)[:800]}")
                continue
            if not summary:
                await mark_state(session, post_id, summary_status="empty")
                continue
            await session.execute(
                update(LinkSnapshot)
                .where(LinkSnapshot.id == snapshot.id)
                .values(summary_short=summary, summary_model=model_name, summary_generated_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
            )
            await mark_state(session, post_id, summary_status="done")
            count += 1
        await session.commit()
    return count


async def summarize_post_links(post_id: int, refresh: bool = False) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        prompt_config = None
        if settings.summary_backend == "yandexgpt":
            prompt_version = await ensure_active_prompt_version(session, name=LINK_SUMMARY_PROMPT)
            prompt_config = prompt_config_from_version(prompt_version)
        stmt = (
            select(LinkSnapshot, PostLink.post_id)
            .join(PostLink, PostLink.id == LinkSnapshot.link_id)
            .where(
                PostLink.post_id == post_id,
                PostLink.url_type.in_(["article", "arxiv", "telegram"]),
                LinkSnapshot.extracted_text.is_not(None),
            )
            .order_by(PostLink.position_index.nulls_last(), LinkSnapshot.id)
        )
        if not refresh:
            stmt = stmt.where(LinkSnapshot.summary_short.is_(None))
        result = await session.execute(stmt)
        rows = list(result.all())
        for snapshot, source_post_id in rows:
            try:
                if settings.summary_backend == "yandexgpt":
                    summary, model_name = await yandex_summary(snapshot, settings, prompt_config or {})
                else:
                    summary, model_name = summarize_text(snapshot.extracted_text, snapshot.title, settings)
            except YandexGPTError as exc:
                await mark_state(session, source_post_id, summary_status="failed", last_error=f"summary_yandexgpt: {str(exc)[:800]}")
                continue
            if not summary:
                await mark_state(session, source_post_id, summary_status="empty")
                continue
            await session.execute(
                update(LinkSnapshot)
                .where(LinkSnapshot.id == snapshot.id)
                .values(summary_short=summary, summary_model=model_name, summary_generated_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
            )
            await mark_state(session, source_post_id, summary_status="done")
            count += 1
        await session.commit()
    return count


@app.command("summarize-pending")
def summarize_pending_command(
    limit: int = limit_option(100),
    refresh: bool = typer.Option(False, "--refresh", help="Refresh existing link summaries as well as creating missing ones."),
) -> None:
    """Generate summaries for extracted link text."""

    count = run_async(summarize_pending(limit, refresh=refresh))
    safe_echo(f"summaries={count}")


if __name__ == "__main__":
    app()
