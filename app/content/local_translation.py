from __future__ import annotations

from datetime import datetime, timezone

import typer
from sqlalchemy import select, update

from app.config import Settings
from app.content.common import limit_option, run_async, session_factory, settings_or_exit
from app.content.state import mark_state
from app.content.text_utils import clean_text, detect_language
from app.main import safe_echo
from app.models import ContentItem

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Local translation commands."""


def translate_argos(text: str, source_lang: str, target_lang: str) -> str | None:
    try:
        import argostranslate.translate

        translated = argostranslate.translate.translate(text, source_lang, target_lang)
        return translated if translated and translated != text else None
    except Exception:
        return None


def translate_text(text: str | None, source_lang: str | None, settings: Settings) -> str | None:
    cleaned = clean_text(text)
    if not cleaned or not settings.enable_translation:
        return None
    source = source_lang or detect_language(cleaned)
    target = settings.translation_default_target_lang
    if not source or source == target:
        return cleaned
    if settings.translation_backend == "argos":
        return translate_argos(cleaned, source, target)
    return None


async def translate_pending(limit: int) -> int:
    settings = settings_or_exit()
    factory = session_factory(settings)
    count = 0
    async with factory() as session:
        result = await session.execute(
            select(ContentItem)
            .where((ContentItem.translated_title.is_(None)) | (ContentItem.translated_summary.is_(None)))
            .order_by(ContentItem.id)
            .limit(limit)
        )
        items = list(result.scalars())
        for item in items:
            source_lang = item.source_lang or detect_language(item.source_summary or item.main_text)
            translated_title = translate_text(item.title, source_lang, settings)
            translated_summary = translate_text(item.source_summary, source_lang, settings)
            await session.execute(
                update(ContentItem)
                .where(ContentItem.id == item.id)
                .values(
                    source_lang=source_lang,
                    target_lang=settings.translation_default_target_lang,
                    translated_title=translated_title,
                    translated_summary=translated_summary,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await mark_state(session, item.source_post_id, translation_status="done" if source_lang == settings.translation_default_target_lang or translated_title or translated_summary else "unavailable")
            count += 1
        await session.commit()
    return count


@app.command("translate-pending")
def translate_pending_command(limit: int = limit_option(100)) -> None:
    """Translate non-Russian content locally when a local backend is available."""

    count = run_async(translate_pending(limit))
    safe_echo(f"translated={count}")


if __name__ == "__main__":
    app()
